"""Event ingress (FastAPI): Orthanc stable-study webhook + RIS DiagnosticReport poller.

Owner: Pranathi (lead). Trigger map: ARCHITECTURE.md
- POST /webhooks/orthanc : starts one StudyWorkflow per new stable study.
- background poller       : detects RIS sign-off and signals the waiting workflow.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from radagent_common import validate_against, paths
from radagent_common.fhir_client import Fhir2Client
from radagent_common.tracing import now_iso, new_trace_id, new_span_id
from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .ingress_store import IngressStore
from . import activities

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
POLL_INTERVAL_S = int(os.environ.get("RIS_POLL_INTERVAL_S", "30"))
# Reconcile the index against Temporal every N polls (plus once at startup) to evict rows for
# workflows that completed/terminated without ever delivering a report.
RECONCILE_EVERY_POLLS = int(os.environ.get("INGRESS_RECONCILE_EVERY_POLLS", "120"))

_client: Client | None = None
_log = logging.getLogger("orchestrator.ingress")

# Durable report->workflow index + poll cursor (#6): must survive an ingress restart during the
# hours-long human-gate wait, or the sign-off is silently lost. Backed by SQLite at
# INGRESS_STORE_PATH, created lazily so importing this module has no side effect; tests override
# `_STORE` with a temp DB. See orchestrator/ingress_store.py.
_STORE: IngressStore | None = None


def _default_store_path() -> str:
    """Absolute + CWD-independent, so a restart from any working directory finds the same DB.
    Production MUST override INGRESS_STORE_PATH to a durable *mounted volume* — a path inside the
    container's own filesystem is wiped on redeploy, defeating the whole point."""
    return os.environ.get("INGRESS_STORE_PATH") or str(paths.repo_root() / "ingress_state.db")


def _store() -> IngressStore:
    global _STORE
    if _STORE is None:
        _STORE = IngressStore(_default_store_path())
    return _STORE


# Read-only fhir2 client for accession -> patient/order resolution (#11). Lazily constructed so
# importing this module has no side effect; tests override `_FHIR` with a fake.
_FHIR: Fhir2Client | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


async def _temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_TARGET)
    return _client


async def _build_study_context(event: dict) -> dict:
    """Map an Orthanc event to a (schema-valid) StudyContext, resolving the patient + order from the
    accession via fhir2 (#11).

    Resolution is best-effort: on any fhir2 error or a miss we fall back to the `Patient/UNRESOLVED`
    placeholder so the workflow always starts -- ingestion must never fail the PACS. A later stable
    re-fire (see orthanc_webhook) repairs an UNRESOLVED first pass without a restart.
    """
    wf_id = f"wf_{event['orthancStudyId']}"
    patient, order = await _resolve_patient_order(event.get("accessionNumber"))
    return {
        "schemaVersion": "1.0.0",
        "workflowId": wf_id,
        "study": {
            "studyInstanceUID": event["studyInstanceUID"],
            "accessionNumber": event.get("accessionNumber"),
            "orthancStudyId": event["orthancStudyId"],
            "modality": event["modality"],
        },
        "patient": patient,
        "order": order,
        "meta": {
            "traceId": new_trace_id(),
            "spanId": new_span_id(),
            "emittedAt": now_iso(),
            "source": "orchestrator.ingress",
        },
    }


async def _resolve_patient_order(accession: str | None) -> tuple[dict, dict]:
    """(patient, order) blocks for the StudyContext. Resolve real refs from fhir2 by accession; on a
    miss or ANY fhir2 failure, return the UNRESOLVED placeholder -- never fail ingestion (#11)."""
    if accession:
        try:
            resolved = await _fhir().resolve_order_by_accession(accession)
        except Exception:  # noqa: BLE001 - fhir2 down/unreachable must not fail the webhook
            _log.warning("fhir2 resolution failed for accession %s; starting Patient/UNRESOLVED", accession)
            resolved = None
        if resolved:
            return ({"fhirPatientId": resolved["fhirPatientId"]},
                    {"fhirServiceRequestId": resolved["fhirServiceRequestId"]})
    return {"fhirPatientId": "Patient/UNRESOLVED"}, {}


def _index_workflow(ctx: dict) -> None:
    """Record a study's join keys -> workflowId so its finalized report can find it later.

    Ingest resolves the order from the accession (#11), so when fhir2 answers we hold both the
    accession (from the Orthanc event) and the ServiceRequest ref. Index whatever is present.

    The ServiceRequest ref is the robust join. The accession is only the fallback for a study that
    resolved nothing (fhir2 down), and a DICOM accession is an order identifier not guaranteed
    unique per study, so on an accession collision we WARN rather than silently overwrite.
    """
    wf_id = ctx["workflowId"]
    accession = (ctx.get("study") or {}).get("accessionNumber")
    service_request = (ctx.get("order") or {}).get("fhirServiceRequestId")
    keys = [k for k in (accession, service_request) if k]
    if not keys:
        _log.warning("study %s has no join key; its finalized report cannot be matched", wf_id)
    store = _store()
    for key in keys:
        existing = store.workflow_id_for(key)
        if existing and existing != wf_id:
            _log.warning("join key %r re-points %s -> %s (accession not unique?)", key, existing, wf_id)
        store.put_index(key, wf_id)


def _workflow_id_for_report(report: dict) -> str | None:
    """Map a finalized report back to its workflow via the keys recorded at start. Prefer the
    ServiceRequest ref (the robust join from #11); fall back to the accession."""
    store = _store()
    for key in (report.get("serviceRequestRef"), report.get("accessionNumber")):
        if key:
            wf_id = store.workflow_id_for(key)
            if wf_id:
                return wf_id
    return None


async def _process_batch(client: Client, reports: list[dict], skip_ids: set[str]) -> tuple[set[str], list[dict]]:
    """Signal each mapped, not-yet-signalled report to its waiting workflow; return (ids newly
    signalled, mapped reports whose signal FAILED). Reports already signalled at the current
    cursor (`skip_ids`) are deduped. A report with no known workflow is LOGGED and skipped.

    A failed report is returned rather than dropped so `_advance_cursor` can hold the cursor at
    it (#29): the inclusive ge-window then re-returns it next poll — a retry for free. The retry
    naturally stops when the workflow is truly gone: reconciliation evicts its index row, the
    report re-enters as unmapped, and the cursor moves on. Durable dead-letter capture of those
    final drops stays with the rest of #29."""
    signalled: set[str] = set()
    failed: list[dict] = []
    for report in reports:
        report_id = report.get("diagnosticReportId")
        if report_id in skip_ids:
            continue
        wf_id = _workflow_id_for_report(report)
        if not wf_id:
            _log.warning("finalized report %s matched no waiting workflow (dropped)", report_id)
            continue
        try:
            await client.get_workflow_handle(wf_id).signal(StudyWorkflow.report_finalized, report)
            signalled.add(report_id)  # mapping is reclaimed on completion by _reconcile_index
        except Exception:  # noqa: BLE001 - workflow gone/unreachable
            failed.append(report)
            _log.warning("failed to signal workflow %s for report %s; will retry next poll", wf_id, report_id)
    return signalled, failed


def _advance_cursor(cursor: str, high_water: str | None, reports: list[dict], signalled: set[str],
                    failed: list[dict] | None = None) -> tuple[str, set[str]]:
    """Advance to the high-water mark — but never past a mapped report whose signal failed (#29).

    ge{cursor} re-returns reports at the boundary second, so we remember which of those we already
    signalled and drop the rest (older ids fall out of the query window). If the high-water didn't
    move, hold the cursor and keep accumulating dedup ids at this boundary.

    Holding at the earliest failure keeps that report inside the ge-window so the next poll
    retries it; before, a later success in the same batch advanced the cursor past it and the
    sign-off was silently lost. While held back, the dedup set keeps EVERY already-signalled id
    at-or-after the cursor (not just the boundary), so the wider re-scan does not re-signal.
    (End-to-end delivery is still at-least-once — e.g. a crash after signal but before
    save_cursor replays the batch — which is safe because report_finalized is an idempotent
    overwrite; keep it that way.)
    """
    floor = None
    if failed:
        stamps = [r.get("lastUpdatedCursor") for r in failed]
        # A failure we cannot place in time pins the cursor entirely (rare: report with no meta).
        # Clamp at the current cursor: it advances or holds, never retreats.
        floor = cursor if None in stamps else max(min(stamps), cursor)
    target = high_water
    if floor is not None and (target is None or floor < target):
        target = floor
    if not target or target == cursor:
        return cursor, signalled
    kept = {
        r["diagnosticReportId"] for r in reports
        if (r.get("lastUpdatedCursor") or "") >= target and r["diagnosticReportId"] in signalled
    }
    return target, kept


async def _is_open(client: Client, wf_id: str) -> bool:
    """Whether a workflow is still running. Only an AFFIRMATIVE answer counts as closed: a
    NOT_FOUND (workflow truly gone) or a successful describe showing a non-RUNNING status.
    UNREACHABLE IS NOT CLOSED (#29): during a Temporal outage — the very condition that makes
    signals fail and the poller hold its cursor — a reconcile sweep would otherwise evict every
    index row, turning each held retry into a permanent sign-off loss and stranding every other
    in-flight study. On a transport error we assume open, keep the row, and let a later sweep
    decide."""
    try:
        desc = await client.get_workflow_handle(wf_id).describe()
    except RPCError as e:
        return e.status != RPCStatusCode.NOT_FOUND  # gone -> closed; anything else -> assume open
    except Exception:  # noqa: BLE001 - unexpected transport failure: same stance, assume open
        return True
    return desc.status == WorkflowExecutionStatus.RUNNING


async def _reconcile_index(client: Client) -> int:
    """Evict index rows whose workflow is no longer open, the durable and restart-safe form of
    'evict on workflow completion' (#6). Reconciliation is the only eviction path. A delivered
    report keeps its row while the workflow runs so an addendum can still route, and this sweep
    reclaims that row once the workflow closes. It also reclaims studies that never delivered a
    report (cancelled, QC-rejected, or terminated), so index growth stays bounded to the set of
    studies still awaiting sign-off. Runs at poller startup (the GC point after a restart) and
    periodically. Returns rows pruned."""
    store = _store()
    pruned = 0
    for wf_id in store.indexed_workflow_ids():
        if not await _is_open(client, wf_id):
            store.evict_workflow(wf_id)
            pruned += 1
    if pruned:
        _log.info("index reconcile: evicted %d row(s) for closed/absent workflows", pruned)
    return pruned


# Real-time nudge (#25): the RIS-side hook (OpenMRS module hook or an Atomfeed bridge) POSTs to
# /webhooks/ris/event and the poller sweeps NOW instead of waiting out the interval. The cursor
# sweep stays the single source of truth — a nudge carries no data and can be lost, duplicated,
# or fired spuriously without affecting correctness; interval polling remains the fallback.
# Lazily created so importing this module has no side effect; tests reset `_WAKE`.
_WAKE: asyncio.Event | None = None


def _wake_event() -> asyncio.Event:
    global _WAKE
    if _WAKE is None:
        _WAKE = asyncio.Event()
    return _WAKE


async def _sleep_or_nudge(seconds: float) -> bool:
    """Wait for the next sweep: a full interval, or sooner if the RIS event webhook nudges.
    Returns True when nudged. A nudge that lands DURING a sweep is not lost — the event stays
    set until consumed here, so bursts coalesce into exactly one immediate re-sweep."""
    wake = _wake_event()
    try:
        await asyncio.wait_for(wake.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        wake.clear()


async def _ris_poller() -> None:
    """Poll fhir2 for finalized reports and signal the matching workflows.

    Advances an inclusive high-water cursor and dedups by report id at the boundary, so no
    sign-off is dropped at a shared-second timestamp and none is signalled twice. A RIS event
    nudge (#25) only shortens the wait before a sweep; it never bypasses the cursor.
    """
    store = _store()
    cursor, signalled_at_cursor = store.load_cursor()
    if cursor is None:                                   # fresh start: begin at "now" and persist
        cursor = now_iso()                               # it, so a restart before the first poll
        store.save_cursor(cursor, signalled_at_cursor)   # resumes here, not a later "now" (no gap).
    try:
        await _reconcile_index(await _temporal())  # GC rows for workflows closed during downtime
    except Exception:  # noqa: BLE001 - reconciliation is best-effort; never block delivery
        _log.warning("index reconciliation failed at startup")
    polls = 0
    while True:
        await _sleep_or_nudge(POLL_INTERVAL_S)
        polls += 1
        if polls % RECONCILE_EVERY_POLLS == 0:
            try:
                await _reconcile_index(await _temporal())
            except Exception:  # noqa: BLE001
                _log.warning("periodic index reconciliation failed")
        try:
            reports, high_water = await activities.poll_finalized_reports(cursor)
        except NotImplementedError:
            continue  # fhir2 client not wired in this environment
        except Exception:  # noqa: BLE001 - keep the loop alive
            continue
        failed: list[dict] = []
        if reports:
            newly, failed = await _process_batch(await _temporal(), reports, signalled_at_cursor)
            signalled_at_cursor |= newly
        cursor, signalled_at_cursor = _advance_cursor(cursor, high_water, reports, signalled_at_cursor, failed)
        store.save_cursor(cursor, signalled_at_cursor)  # durable: a restart mid-wait resumes here


@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = asyncio.create_task(_ris_poller())
    yield
    poller.cancel()


app = FastAPI(title="LH-Radiology Orchestrator Ingress", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhooks/ris/event", status_code=202)
async def ris_event() -> dict:
    """RIS-side change hook (#25): nudge the poller to sweep immediately.

    Deliberately takes NO payload: the fhir2 cursor sweep remains the single source of truth,
    so this endpoint is PHI-free, idempotent, and safe for the RIS side to fire on any event
    (or never — interval polling is the unchanged fallback)."""
    _wake_event().set()
    return {"nudged": True}


@app.post("/webhooks/orthanc")
async def orthanc_webhook(event: dict) -> dict:
    try:
        validate_against(event, paths.contracts_dir() / "events" / "orthanc-stable.schema.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    ctx = await _build_study_context(event)
    # (Re)index the join keys so this study's finalized report can find it. On a re-fire this also
    # REPAIRS a first pass that resolved nothing: _build_study_context just re-ran fhir2 resolution,
    # so if fhir2 is back the ServiceRequest ref is now present and gets indexed here (#11).
    _index_workflow(ctx)
    client = await _temporal()
    try:
        await client.start_workflow(
            StudyWorkflow.run,
            ctx,
            id=ctx["workflowId"],
            task_queue=TASK_QUEUE,
        )
    except WorkflowAlreadyStartedError:
        # Orthanc re-fires OnStableStudy for the same study (a late instance reopens it, so it goes
        # stable again). The workflow id is deterministic (wf_<orthancStudyId>), so a duplicate event
        # is normal PACS behaviour, not an error: return 200 with the existing id so the plugin and
        # its retries (#47) see success instead of a 500. The re-index above is the #11 "second
        # chance" -- an UNRESOLVED first pass gets its ServiceRequest join repaired here (index-side)
        # with no restart; the already-running workflow keeps its original ctx.
        _log.info("duplicate stable-study event for %s; re-indexed, workflow already running", ctx["workflowId"])
        return {"started": ctx["workflowId"], "duplicate": True}
    return {"started": ctx["workflowId"]}
