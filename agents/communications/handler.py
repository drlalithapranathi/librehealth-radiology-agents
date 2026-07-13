"""Communications Agent handler — owner: Pranathi (lead).

The real CritCom (Critical-Results Communication) agent (#52). Three skills:
  - comms.dispatch : classify the finding (ACR) + notify the ordering provider + open an ack clock
  - comms.checkAck : read the acknowledgement Task; report status + `overdue`
  - comms.escalate : an unacknowledged critical result goes to the on-call provider

TWO DIFFERENT GATES. Do not confuse them, and do not double-page:
  * #29's escalation ladder is the "radiologist didn't SIGN" gate. Its fired rung arrives here as
    an `escalation` input slice on comms.dispatch. The ladder already chose who/how/how-loudly, so
    those channels are dispatched VERBATIM and NO ack clock is opened -- this is the orchestrator
    paging a human about a report that does not exist yet. There is nothing to acknowledge.
  * checkAck/escalate are the "physician didn't ACK a critical result" gate: a signed report whose
    critical finding was communicated, and nobody confirmed receipt. That loop is what the
    Communication/Task pair in the comms ledger tracks.

Handlers stay pure: radagent_common + siblings only, never a2a.* (golden rule 4). Clinical context
is read from fhir2; the notification and its ack are written to the comms ledger (golden rule 2 --
the A2A payload carries IDs, and content is fetched from source).

The agent NEVER self-fires a timer. It opens the ack clock and reports the deadline; the
orchestrator owns the durable wait and calls back on comms.checkAck / comms.escalate (MR 4).

Contracts: contracts/skills/comms.{dispatch,checkAck,escalate}.schema.json
"""
from __future__ import annotations

import logging

from radagent_common.comms_ledger import CommsLedgerClient
from radagent_common.fhir_client import Fhir2Client
from radagent_common.tracing import now_iso

from classifier import ACRCategory, classify
from tools import (
    ack_state,
    dispatch_communication,
    escalate_to_on_call,
    open_ack_task,
    resolve_on_call_provider,
    resolve_ordering_provider,
)

AGENT_VERSION = "0.2.0"
_log = logging.getLogger("agents.communications")

_ROUTINE_CHANNEL = "ehr-inbox"       # every finalized report posts to the ordering provider's inbox
_CRITICAL_CHANNEL = "oncall-pager"   # critical results also page (closed-loop comms)

# Lazily constructed so importing this module has no side effect; tests/harness override these.
_FHIR: Fhir2Client | None = None
_LEDGER: CommsLedgerClient | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


def _ledger() -> CommsLedgerClient:
    global _LEDGER
    if _LEDGER is None:
        _LEDGER = CommsLedgerClient()
    return _LEDGER


def _refs(payload: dict) -> tuple[str, str]:
    """(patient_ref, service_request_ref) -- the explicit inputs when the orchestrator resolved
    them, else the StudyContext envelope's."""
    ctx = payload["studyContext"]
    patient = payload.get("patientId") or (ctx.get("patient") or {}).get("fhirPatientId") or ""
    order = (payload.get("serviceRequestId")
             or (ctx.get("order") or {}).get("fhirServiceRequestId") or "")
    return patient, order


def _out(payload: dict, **fields) -> dict:
    return {"schemaVersion": "1.0.0", "workflowId": payload["studyContext"]["workflowId"],
            "agentVersion": AGENT_VERSION, **fields}


# --- comms.dispatch -------------------------------------------------------------------

async def _dispatch(payload: dict) -> dict:
    escalation = payload.get("escalation") or {}
    if escalation:
        # A fired sign-off ladder rung (#29) -- the OTHER gate. The ladder picked the channels, so
        # send them as asked. No Communication, no ack clock: there is no signed report to
        # acknowledge, and opening one here would put the same human on two clocks at once.
        return _out(
            payload,
            dispatchStatus="SENT",
            channelResults=[{"channel": c, "status": "SENT"} for c in escalation["channels"]],
            dispatchedAt=now_iso(),
        )

    result = classify(payload.get("impression") or {}, payload.get("verification") or {})

    # Routine result: it posts to the EHR inbox like any report, and there is nothing to
    # acknowledge -- opening an ack clock on a normal chest X-ray is how alert fatigue starts.
    if not result.is_critical:
        return _out(
            payload,
            dispatchStatus="SENT",
            acrCategory=result.category.value,
            channelResults=[{"channel": _ROUTINE_CHANNEL, "status": "SENT"}],
            dispatchedAt=now_iso(),
        )

    patient_ref, order_ref = _refs(payload)
    recipient = await resolve_ordering_provider(_fhir(), order_ref)
    if not recipient:
        # No requester on the order (e.g. a study ingested unresolved, #11). A critical finding
        # with nobody to tell must not be silently dropped -- go straight to on-call.
        recipient = await resolve_on_call_provider(_ledger())
        _log.warning("no requester on %s; addressing the critical result to on-call (%s)",
                     order_ref or "<no order>", recipient)
    if not recipient:
        # Nobody to tell, at all. SKIPPED is the honest answer -- reporting SENT would claim a page
        # that never happened. The orchestrator's own #29 ladder still pages a human.
        _log.error("no recipient for a %s finding on %s", result.category.value, order_ref)
        return _out(
            payload,
            dispatchStatus="SKIPPED",
            acrCategory=result.category.value,
            channelResults=[],
            dispatchedAt=now_iso(),
        )

    comm = await dispatch_communication(
        _ledger(),
        patient_ref=patient_ref,
        service_request_ref=order_ref,
        recipient_ref=recipient,
        acr_category=result.category.value,
        finding=result.finding,
    )
    task, deadline = await open_ack_task(
        _ledger(),
        communication_ref=f"Communication/{comm.id}",
        patient_ref=patient_ref,
        owner_ref=recipient,
        ack_minutes=result.ack_minutes or 60,
    )
    return _out(
        payload,
        dispatchStatus="SENT",
        acrCategory=result.category.value,
        communicationId=comm.id or "",
        taskId=task.id or "",
        deadline=deadline.isoformat(),
        recipient=recipient,
        channelResults=[{"channel": _ROUTINE_CHANNEL, "status": "SENT"},
                        {"channel": _CRITICAL_CHANNEL, "status": "SENT"}],
        dispatchedAt=now_iso(),
    )


# --- comms.checkAck -------------------------------------------------------------------

async def _check_ack(payload: dict) -> dict:
    task_id = payload["taskId"]
    status, deadline, overdue = ack_state(await _ledger().get_task(task_id))
    return _out(
        payload,
        taskId=task_id,
        ackStatus=status,
        deadline=deadline.isoformat() if deadline else "",
        overdue=overdue,
        checkedAt=now_iso(),
    )


# --- comms.escalate -------------------------------------------------------------------

async def _escalate(payload: dict) -> dict:
    task_id = payload["taskId"]
    patient_ref, order_ref = _refs(payload)

    # Re-derive the urgency from the Task's own Communication rather than trusting an input: the
    # escalation must carry the SAME category as the notification nobody answered.
    task = await _ledger().get_task(task_id)
    acr, finding = ACRCategory.CAT1.value, "unacknowledged critical result"
    if task.focus and task.focus.reference:
        comm = await _ledger().get_communication(task.focus.reference.split("/")[-1])
        if comm.category and comm.category[0].coding:
            acr = comm.category[0].coding[0].code or acr
        finding = comm.finding_summary or finding

    result = await escalate_to_on_call(
        _ledger(),
        overdue_task_id=task_id,
        patient_ref=patient_ref,
        service_request_ref=order_ref,
        acr_category=acr,
        finding=finding,
        ack_minutes=int(payload.get("ackMinutes") or 60),
    )
    return _out(payload, escalatedAt=now_iso(), **result)


_SKILLS = {
    "comms.dispatch": _dispatch,
    "comms.checkAck": _check_ack,
    "comms.escalate": _escalate,
}


async def handle(skill_id: str, payload: dict) -> dict:
    fn = _SKILLS.get(skill_id)
    if fn is None:
        raise ValueError(f"unexpected skill {skill_id}")
    return await fn(payload)
