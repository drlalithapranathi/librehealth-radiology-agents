"""Worklist Triage handler — owner: Parvati.

v1 = transparent rule-of-thumb scoring from order + study signals. Real model later.
Input  : { studyContext }
Output : contracts/skills/triage.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"
_STAT_REASONS = {"I21", "I63", "S06", "R57"}  # MI, stroke, intracranial injury, shock (examples)


async def handle(skill_id: str, payload: dict) -> dict:
    assert skill_id == "triage.score", f"unexpected skill {skill_id}"
    ctx = payload["studyContext"]
    order = ctx.get("order", {})
    modality = ctx["study"].get("modality", "")
    reasons = order.get("reasonCode", [])

    score = 50
    rationale = [f"modality={modality}"]
    if order.get("priority") in ("stat", "asap"):
        score += 30
        rationale.append(f"order priority={order['priority']}")
    if any(r.split(".")[0] in _STAT_REASONS for r in reasons):
        score += 25
        rationale.append("reason code maps to time-critical pathway")
    if modality in ("CT", "MR"):
        score += 5

    score = max(0, min(100, score))
    tier = "STAT" if score >= 85 else "URGENT" if score >= 65 else "ROUTINE"

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "priorityScore": score,
        "priorityTier": tier,
        "rationale": rationale,
        "agentVersion": AGENT_VERSION,
        "computedAt": now_iso(),
    }
