"""Communications Agent handler — owner: Pranathi (lead).

The existing LH Communications service, being grown into the real CritCom (Critical-Results
Communication) agent (#52). It serves three skills:
  - comms.dispatch : classify the finding (ACR) + page the right provider + open an ack clock
  - comms.checkAck : poll the acknowledgement Task for a sent notification
  - comms.escalate : escalate an unacknowledged critical result to the on-call provider

v1 is a stub: comms.dispatch keeps the #17 channel-selection behaviour (real Communication/Task
writes to the comms-ledger land in MR 3 with the FHIR wiring); comms.checkAck / comms.escalate
return contract-valid stubs. Handlers stay pure -- import radagent_common + siblings only,
never a2a.*.

A sign-off escalation rung (#29) still arrives on comms.dispatch as an `escalation` slice
(contracts/escalation-policy.schema.json $defs/dispatchEscalation): that ladder already chose
who/how, so its channels are dispatched verbatim instead of being re-derived from urgency here.
This is the orchestrator's "radiologist didn't SIGN" gate -- a DIFFERENT gate from CritCom's own
"physician didn't ACK a critical result" loop (checkAck/escalate). Do not double-page.

Contracts: contracts/skills/comms.{dispatch,checkAck,escalate}.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"
_ROUTINE_CHANNEL = "ehr-inbox"       # every finalized report posts to the ordering provider's inbox
_CRITICAL_CHANNEL = "oncall-pager"   # critical results also page the on-call (closed-loop comms)


def _is_critical(impression: dict, verification: dict) -> bool:
    """Critical if the impression flagged a critical finding or verification failed."""
    return bool(impression.get("criticalFlags")) or verification.get("verificationStatus") == "FAIL"


def _dispatch(payload: dict) -> dict:
    ctx = payload["studyContext"]
    impression = payload.get("impression") or {}
    verification = payload.get("verification") or {}
    escalation = payload.get("escalation") or {}

    if escalation:
        # A fired sign-off ladder rung (#29): the ladder already picked who/how, so dispatch its
        # channels as requested rather than re-deriving them from urgency.
        channels = list(escalation["channels"])
    else:
        channels = [_ROUTINE_CHANNEL]
        if _is_critical(impression, verification):
            channels.append(_CRITICAL_CHANNEL)
    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "dispatchStatus": "SENT",
        "channelResults": [{"channel": c, "status": "SENT"} for c in channels],
        "agentVersion": AGENT_VERSION,
        "dispatchedAt": now_iso(),
    }


def _check_ack(payload: dict) -> dict:
    # Stub: no live comms-ledger yet (MR 2/3) -> report the ack as still pending, not overdue.
    return {
        "schemaVersion": "1.0.0",
        "workflowId": payload["studyContext"]["workflowId"],
        "taskId": payload.get("taskId", ""),
        "ackStatus": "REQUESTED",
        "deadline": now_iso(),
        "overdue": False,
        "agentVersion": AGENT_VERSION,
        "checkedAt": now_iso(),
    }


def _escalate(payload: dict) -> dict:
    # Stub: no on-call directory / comms-ledger yet (MR 2/3) -> nothing to escalate to.
    return {
        "schemaVersion": "1.0.0",
        "workflowId": payload["studyContext"]["workflowId"],
        "escalated": False,
        "reason": "no on-call configured (stub)",
        "agentVersion": AGENT_VERSION,
        "escalatedAt": now_iso(),
    }


_SKILLS = {
    "comms.dispatch": _dispatch,
    "comms.checkAck": _check_ack,
    "comms.escalate": _escalate,
}


async def handle(skill_id: str, payload: dict) -> dict:
    fn = _SKILLS.get(skill_id)
    if fn is None:
        raise ValueError(f"unexpected skill {skill_id}")
    return fn(payload)
