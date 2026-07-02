"""Impression Generation handler — owner: Chaitra.

Timing-agnostic (the timing note in impression-generation/CLAUDE.md): same skill whether invoked post-sign (v1 safety-net)
or pre-sign (M2 assist). v1 returns a deterministic stub draft.
Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    findings_text = report.get("findingsText", "")

    is_critical = any(word in findings_text.lower() for word in
                      ["dissection", "pneumothorax", "hemorrhage", "occlusion", "rupture"])

    impression = (
        "Findings are consistent with aortic dissection. Urgent surgical consultation recommended."
        if is_critical else
        "No acute cardiopulmonary findings identified. Stable appearance."
    )

    critical_flags = (
        [{"label": "aortic dissection", "severity": "critical"}]
        if is_critical else []
    )

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "impressionText": impression,
        "structuredFindings": [],
        "recommendations": [{"text": "Urgent surgical consultation"} if is_critical else {"text": "Routine follow-up"}],
        "criticalFlags": critical_flags,
        "agentVersion": AGENT_VERSION,
        "generatedAt": now_iso(),
    }