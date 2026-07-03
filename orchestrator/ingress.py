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

from radagent_common import validate_against, paths
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


async def _temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_TARGET)
    return _client


def _build_study_context(event: dict) -> dict:
    """Map an Orthanc event to a (schema-valid) StudyContext.

    TODO(M1): resolve patient.fhirPatientId + order via fhir2 from the accession number.
    For M0 we emit placeholders so the workflow can start end-to-end.
    """
    wf_id = f"wf_{event['orthancStudyId']}"
    return {
        "schemaVersion": "1.0.0",
        "workflowId": wf_id,
        "study": {
            "studyInstanceUID": event["studyInstanceUID"],
            "accessionNumber": event.get("accessionNumber"),
            "orthancStudyId": event["orthancStudyId"],
            "modality": event["modality"],
        },
        "patient": {"fhirPatientId": "Patient/UNRESOLVED"},  # TODO(M1): fhir2 lookup
        "order": {},
        "meta": {
            "traceId": new_trace_id(),
            "spanId": new_span_id(),
            "emittedAt": now_iso(),
            "source": "orchestrator.ingress",
        },
    }


def _index_workflow(ctx: dict) -> None:
    """Record a study's join keys -> workflowId so its finalized report can find it later.

    At start we may have the accession (from the Orthanc event) and — once #11 resolves the
    order — the ServiceRequest ref. Index whatever is present.

    NOTE(M1): the accession is currently the ONLY key we get (order is resolved in #11) and a
    DICOM accession is an order identifier, not guaranteed unique per study — so we WARN rather
    than silently overwrite on collision. The robust ServiceRequest join lights up with #11.
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
    ServiceRequest ref (robust once #11 lands); fall back to the accession."""
    store = _store()
    for key in (report.get("serviceRequestRef"), report.get("accessionNumber")):
        if key:
            wf_id = store.workflow_id_for(key)
            if wf_id:
                return wf_id
    return None


async def _process_batch(client: Client, reports: list[dict], skip_ids: set[str]) -> set[str]:
    """Signal each mapped, not-yet-signalled report to its waiting workflow; return the ids newly
    signalled. Reports already signalled at the current cursor (`skip_ids`) are deduped. A report
    with no known workflow is LOGGED and skipped.

    KNOWN GAP (#29, durable retry / dead-letter): a *mapped* report whose signal raises is logged
    but not retried here. A boundary failure is safe (the inclusive ge-cursor re-returns it next
    poll), but if a LATER report in the same batch succeeds and advances the cursor past a failed
    earlier one, that report falls below the ge-window and its sign-off is lost. #6 makes the
    cursor durable (so downtime reports are no longer skipped) but does not close this retry gap."""
    signalled: set[str] = set()
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
            _log.warning("failed to signal workflow %s for report %s", wf_id, report_id)
    return signalled


def _advance_cursor(cursor: str, high_water: str | None, reports: list[dict], signalled: set[str]) -> tuple[str, set[str]]:
    """Advance to the high-water mark; keep only the ids AT the new boundary for dedup.

    ge{cursor} re-returns reports at the boundary second, so we remember which of those we already
    signalled and drop the rest (older ids fall out of the query window). If the high-water didn't
    move, hold the cursor and keep accumulating dedup ids at this boundary.
    """
    if not high_water or high_water == cursor:
        return cursor, signalled
    kept = {
        r["diagnosticReportId"] for r in reports
        if r.get("lastUpdatedCursor") == high_water and r["diagnosticReportId"] in signalled
    }
    return high_water, kept


async def _is_open(client: Client, wf_id: str) -> bool:
    """Whether a workflow is still running. A missing/gone workflow is treated as closed."""
    try:
        desc = await client.get_workflow_handle(wf_id).describe()
    except Exception:  # noqa: BLE001 - not found / unreachable-as-closed -> safe to reclaim
        return False
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


async def _ris_poller() -> None:
    """Poll fhir2 for finalized reports and signal the matching workflows.

    Advances an inclusive high-water cursor and dedups by report id at the boundary, so no
    sign-off is dropped at a shared-second timestamp and none is signalled twice.
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
        await asyncio.sleep(POLL_INTERVAL_S)
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
        if reports:
            signalled_at_cursor |= await _process_batch(await _temporal(), reports, signalled_at_cursor)
        cursor, signalled_at_cursor = _advance_cursor(cursor, high_water, reports, signalled_at_cursor)
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


@app.post("/webhooks/orthanc")
async def orthanc_webhook(event: dict) -> dict:
    try:
        validate_against(event, paths.contracts_dir() / "events" / "orthanc-stable.schema.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    ctx = _build_study_context(event)
    _index_workflow(ctx)  # remember the join keys so this study's finalized report can find it
    client = await _temporal()
    await client.start_workflow(
        StudyWorkflow.run,
        ctx,
        id=ctx["workflowId"],
        task_queue=TASK_QUEUE,
    )
    return {"started": ctx["workflowId"]}
