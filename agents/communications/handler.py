"""Communications Agent handler — owner: Pranathi (lead).

The existing LH Communications service, conformed to the A2A `comms.dispatch` contract (#17).
v1 chooses notification channels from the study's urgency and reports a per-channel dispatch;
real channel delivery (EHR inbox, pager, SMS) is stubbed until M3. A sign-off escalation rung
(#29) arrives as an `escalation` slice (contracts/escalation-policy.schema.json
$defs/dispatchEscalation): the ladder already chose who/how, so its channels are dispatched
as requested instead of being re-derived from urgency here.

Input  : { studyContext, report?, impression?, verification?, escalation? }
Output : contracts/skills/comms.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"
_ROUTINE_CHANNEL = "ehr-inbox"       # every finalized report posts to the ordering provider's inbox
_CRITICAL_CHANNEL = "oncall-pager"   # critical results also page the on-call (closed-loop comms)


def _is_critical(impression: dict, verification: dict) -> bool:
    """Critical if the impression flagged a critical finding or verification failed."""
    return bool(impression.get("criticalFlags")) or verification.get("verificationStatus") == "FAIL"


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "comms.dispatch":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    impression = payload.get("impression") or {}
    verification = payload.get("verification") or {}
    escalation = payload.get("escalation") or {}

    if escalation:
        channels = list(escalation["channels"])
    else:
        channels = [_ROUTINE_CHANNEL]
        if _is_critical(impression, verification):
            channels.append(_CRITICAL_CHANNEL)
    channel_results = [{"channel": c, "status": "SENT"} for c in channels]

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "dispatchStatus": "SENT",
        "channelResults": channel_results,
        "agentVersion": AGENT_VERSION,
        "dispatchedAt": now_iso(),
    }
