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
    assert skill_id == "impression.generate", f"unexpected skill {skill_id}"
    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    # TODO(M2): call an LLM with findings + priors + AI output to draft a real impression.
    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "impressionText": "[stub impression] No acute findings identified.",
        "structuredFindings": [],
        "recommendations": [],
        "criticalFlags": [],
        "agentVersion": AGENT_VERSION,
        "generatedAt": now_iso(),
    }
