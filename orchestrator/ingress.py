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

from fastapi import FastAPI, Header, HTTPException
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from radagent_common import validate_against, paths
from radagent_common.client import parse_push_callback
from radagent_common.fhir_client import Fhir2Client
from radagent_common.validation import validate_skill_output
from radagent_common.tracing import now_iso, new_trace_id, new_span_id, init_tracing, tracing_enabled
from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from .ingress_store import IngressStore, default_store_path
from . import activities

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
POLL_INTERVAL_S = int(os.environ.get("RIS_POLL_INTERVAL_S", "30"))
# Shared secret for A2A push callbacks (#24): agents echo it in X-A2A-Notification-Token.
# Empty (the default) accepts unauthenticated callbacks — dev/compose posture, same as the
# Orthanc webhook; set it in any deployment that leaves the compose network.
A2A_CALLBACK_TOKEN = os.environ.get("A2A_CALLBACK_TOKEN", "")
# Reconcile the index against Temporal every N polls (plus once at startup) to evict rows for
# workflows that completed/terminated without ever delivering a report.
RECONCILE_EVERY_POLLS = int(os.environ.get("INGRESS_RECONCILE_EVERY_POLLS", "120"))
# A failing fhir2 poll is warned on the first failure, then every Nth, so a persistent outage
# (fhir2 down, credentials rejected — #53) stays visible without flooding the log every poll.
# Clamped to >= 1: a 0 would make the throttle modulo raise inside the poller's own exception
# handler, permanently killing the loop the throttle exists to keep observable.
FAILED_POLLS_PER_WARNING = max(1, int(os.environ.get("INGRESS_FAILED_POLLS_PER_WARNING", "10")))

_client: Client | None = None
_log = logging.getLogger("orchestrator.ingress")

# Durable report->workflow index + poll cursor (#6): must survive an ingress restart during the
# hours-long human-gate wait, or the sign-off is silently lost. Backed by SQLite at
# INGRESS_STORE_PATH, created lazily so importing this module has no side effect; tests override
# `_STORE` with a temp DB. See orchestrator/ingress_store.py.
_STORE: IngressStore | None = None


def _default_store_path() -> str:
    """Absolute + CWD-independent, so a restart from any working directory finds the same DB.

    Delegates to ingress_store so the worker's dead-letter activity (#54) resolves the SAME file
    from the other process in this container — one store, one /admin/dead-letters.
    """
    return default_store_path()


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
        # OTel (#28): when enabled, the interceptor spans workflow starts/signals and injects trace
        # context into Temporal headers, linking the webhook span to the worker-side workflow span.
        interceptors: list = []
        if tracing_enabled():
            from temporalio.contrib.opentelemetry import TracingInterceptor
            interceptors = [TracingInterceptor()]
        _client = await Client.connect(TEMPORAL_TARGET, interceptors=interceptors)
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


# The order fields beyond the join ref that the StudyContext carries. `triage.score` scores on both
# and can do nothing without them, so dropping them here silently demotes every study to ROUTINE --
# which is exactly what ingress did until #61.
_ORDER_SIGNALS = ("priority", "reasonCode")


async def _resolve_patient_order(accession: str | None) -> tuple[dict, dict]:
    """(patient, order) blocks for the StudyContext. Resolve the real refs -- and the order's
    urgency signals, which triage scores on (#61) -- from fhir2 by accession; on a miss or ANY fhir2
    failure, return the UNRESOLVED placeholder -- never fail ingestion (#11).

    An order that carries no priority and no reason code resolves to the join ref alone; triage
    treats an absent priority as neutral, which is honest. What is NOT honest is dropping the ones
    that were there.
    """
    if accession:
        try:
            resolved = await _fhir().resolve_order_by_accession(accession)
        except Exception:  # noqa: BLE001 - fhir2 down/unreachable must not fail the webhook
            _log.warning("fhir2 resolution failed for accession %s; starting Patient/UNRESOLVED", accession)
            resolved = None
        if resolved:
            order = {"fhirServiceRequestId": resolved["fhirServiceRequestId"]}
            # Copy by allow-list: `order` is additionalProperties:false, so an unexpected key from
            # a future resolver would fail StudyContext validation and drop the study.
            order.update({k: resolved[k] for k in _ORDER_SIGNALS if resolved.get(k)})
            return {"fhirPatientId": resolved["fhirPatientId"]}, order
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
    naturally stops when the workflow is truly gone: reconciliation evicts its index row and the
    report re-enters as unmapped. Failed attempts are tracked durably so that final unmapped
    re-entry is recognized as OURS and captured as a dead letter (#29) — a permanently dropped
    sign-off a human must see — instead of blending into the routine "never ours" fhir2 noise."""
    store = _store()
    signalled: set[str] = set()
    failed: list[dict] = []
    for report in reports:
        report_id = report.get("diagnosticReportId")
        if report_id in skip_ids:
            continue
        wf_id = _workflow_id_for_report(report)
        if not wf_id:
            was_failing = store.failed_signal_for(report_id) if report_id else None
            if was_failing:
                store.add_dead_letter(
                    report_id, was_failing["workflowId"], was_failing["attempts"],
                    "workflow evicted while its sign-off signal was still failing", now_iso())
                store.clear_failed_signal(report_id)
                _log.error("DEAD LETTER: sign-off %s for workflow %s dropped after %d failed "
                           "attempt(s); see /admin/dead-letters", report_id,
                           was_failing["workflowId"], was_failing["attempts"])
            else:
                _log.warning("finalized report %s matched no waiting workflow (dropped)", report_id)
            continue
        try:
            await client.get_workflow_handle(wf_id).signal(StudyWorkflow.report_finalized, report)
            signalled.add(report_id)  # mapping is reclaimed on completion by _reconcile_index
            store.clear_failed_signal(report_id)  # delivered: retire any failure record
        except Exception:  # noqa: BLE001 - workflow gone/unreachable
            failed.append(report)
            if report_id:  # a report with no id can't be tracked; the held cursor still retries it
                store.record_failed_signal(report_id, wf_id, now_iso())
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
    consecutive_failures = 0
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
            # Swallowing keeps the loop alive, but a fhir2 that is down (or rejecting our
            # credentials, #53) must not be silent: every failed poll is a window in which a
            # radiologist sign-off goes undetected. Warn on the first failure, then throttle.
            consecutive_failures += 1
            if consecutive_failures == 1 or consecutive_failures % FAILED_POLLS_PER_WARNING == 0:
                _log.warning("fhir2 poll failed (%d consecutive); sign-off detection is stalled",
                             consecutive_failures, exc_info=True)
            continue
        if consecutive_failures:
            _log.warning("fhir2 poll recovered after %d consecutive failure(s)", consecutive_failures)
            consecutive_failures = 0
        failed: list[dict] = []
        if reports:
            newly, failed = await _process_batch(await _temporal(), reports, signalled_at_cursor)
            signalled_at_cursor |= newly
        cursor, signalled_at_cursor = _advance_cursor(cursor, high_water, reports, signalled_at_cursor, failed)
        store.save_cursor(cursor, signalled_at_cursor)  # durable: a restart mid-wait resumes here


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast on an invalid FHIR2_BASIC_* pair (#53). Every live consumer constructs the
    # client inside a swallow-and-continue path, where the constructor's ValueError would
    # masquerade as an fhir2 outage forever — startup is the only place it can be loud.
    Fhir2Client()
    poller = asyncio.create_task(_ris_poller())
    yield
    poller.cancel()


app = FastAPI(title="LH-Radiology Orchestrator Ingress", lifespan=lifespan)

# OpenTelemetry (#28): span the webhook + poller; the Temporal client interceptor in _temporal()
# then propagates context to the workflow. Fully off unless OTel is configured; all imports lazy
# so the module (and its tests) never require the [otel] extra.
#
# Instrumented HERE, at import, and NOT inside the lifespan: instrument_app() injects ASGI
# middleware, and by the time the lifespan body runs Starlette has already built and cached its
# middleware stack — the call would be silently ignored and the ingress would export no spans at
# all (no error, no warning). The HTTPX instrumentation is process-global, so it rides along.
if tracing_enabled():
    init_tracing("orchestrator-ingress")
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()  # fhir2 poller calls inject the traceparent


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


@app.get("/admin/dead-letters")
async def dead_letters() -> dict:
    """Everything the pipeline permanently gave up on. Rows carry IDs only (lean-reference, no
    PHI). Empty is the healthy state; anything here needs a human. Read `kind`:

    * `signoff-drop` (#29) — the RIS delivered a finalized report, it mapped to a workflow, every
      signal attempt failed, and the workflow closed before one landed. Reconcile the study in the
      RIS against the orchestrator's history.
    * `escalation-policy-load-failure` (#54) — a sign-off gate could not load its escalation
      ladder, so it fell back to a single flat page. The study is still escalated and readable, but
      escalation is DEGRADED until the policy is fixed: check escalation-policy.yaml and any
      ESCALATION_POLICY_PATH override.
    """
    letters = _store().dead_letters()
    return {"count": len(letters), "deadLetters": letters}


# Artifact parts buffered per push task until its terminal event arrives (#24): the SDK sender
# POSTs the result (artifactUpdate) and the terminal state (statusUpdate) as SEPARATE callbacks.
# In-memory on purpose: entries are popped at the terminal event. If the artifact half is lost
# (one un-retried POST, or an ingress restart between the two), the surviving terminal COMPLETED
# has no result to relay and is reported to the workflow as a failure — the workflow re-runs the
# skill (bounded; see StudyWorkflow._call_push). Size-capped oldest-first because a task whose
# terminal never arrives would otherwise leak its parts forever — and because with the token
# unset (dev posture) this endpoint is unauthenticated, an uncapped dict is a free memory-DoS.
# Tests reset this dict.
_PUSH_PARTS: dict[str, list] = {}
_PUSH_PARTS_CAP = int(os.environ.get("A2A_PUSH_BUFFER_CAP", "1024"))


@app.post("/callbacks/a2a/{workflow_id}", status_code=202)
async def a2a_push_callback(
    workflow_id: str,
    body: dict,
    skill: str = "",
    x_a2a_notification_token: str = Header(default=""),
) -> dict:
    """A2A push-notification receiver (#24): an agent reports progress on a push-mode skill.

    Non-terminal events are acknowledged and ignored; the artifact event's data parts are
    buffered; the terminal event is relayed to the workflow as a `skill_completed` signal keyed
    by taskId. The workflowId and skillId ride in the callback URL — minted by
    start_agent_skill_activity — so no task->workflow index is needed. A well-behaved agent
    validated its output before emitting it, but this endpoint may be reachable by others (the
    token is optional), so a delivered result is re-validated against the skill's contract here
    and relayed as a failure if it doesn't conform. A relay the workflow never receives (dropped
    POST, Temporal briefly down) is recovered by the workflow re-running the skill after its
    wait times out."""
    if A2A_CALLBACK_TOKEN and x_a2a_notification_token != A2A_CALLBACK_TOKEN:
        raise HTTPException(status_code=401, detail="bad callback token")
    try:
        parsed = parse_push_callback(body)
    except Exception as e:  # noqa: BLE001 - not a StreamResponse -> reject, don't 500
        raise HTTPException(status_code=422, detail=f"unparseable push callback: {e}")
    if parsed is None:
        return {"ignored": "non-terminal event"}
    task_id = parsed["taskId"]
    if parsed["kind"] == "artifact":
        if task_id not in _PUSH_PARTS:
            while len(_PUSH_PARTS) >= _PUSH_PARTS_CAP:  # evict oldest orphan (insertion order)
                _PUSH_PARTS.pop(next(iter(_PUSH_PARTS)))
        _PUSH_PARTS.setdefault(task_id, []).extend(parsed["parts"])
        return {"buffered": task_id}

    parts = parsed["parts"] or _PUSH_PARTS.pop(task_id, [])
    _PUSH_PARTS.pop(task_id, None)  # failure path cleanup: don't leak buffered parts
    completed = parsed["state"] == "TASK_STATE_COMPLETED" and bool(parts)
    if completed and skill:
        try:
            validate_skill_output(skill, parts[0])
        except Exception:  # noqa: BLE001 - non-conforming/forged result (log IDs only, no values)
            _log.warning("push result for wf=%s task=%s failed the %s output contract; "
                         "relaying as failure", workflow_id, task_id, skill)
            completed = False
    event = ({"taskId": task_id, "result": parts[0]} if completed
             else {"taskId": task_id, "failed": True})
    try:
        client = await _temporal()
    except Exception:  # noqa: BLE001 - Temporal briefly down: tell the sender, don't 500
        _log.warning("push callback for %s task %s: temporal unavailable", workflow_id, task_id)
        raise HTTPException(status_code=503, detail="temporal unavailable")
    try:
        await client.get_workflow_handle(workflow_id).signal(
            StudyWorkflow.skill_completed, event)
    except Exception:  # noqa: BLE001 - workflow gone: the workflow-side wait timeout re-runs
        _log.warning("push callback for %s task %s could not be signalled", workflow_id, task_id)
        raise HTTPException(status_code=404, detail="workflow not found")
    return {"relayed": task_id}


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
