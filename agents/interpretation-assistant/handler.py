"""Interpretation Assistant handler — owner: Chaitra.

v1 = tool REGISTRY that selects by modality/study-type and returns STUBBED results.
M3: wire real CAD/detection tools behind the same registry interface.
Input  : { studyContext }
Output : contracts/skills/interpretation.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso
from registry import select_tools

AGENT_VERSION = "0.1.0"


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "interpretation.runTools":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    modality = ctx["study"].get("modality", "")
    desc = ctx["study"].get("studyDescription", "")
    tools = select_tools(modality, desc)

    tools_selected = [{"toolId": t, "version": "stub-0", "status": "STUBBED"} for t in tools]
    findings = [{"toolId": t, "label": "", "confidence": None, "evidenceRef": None, "status": "STUBBED"} for t in tools]

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "toolsSelected": tools_selected,
        "findings": findings,
        "overallStatus": "STUBBED",
        "agentVersion": AGENT_VERSION,
        "ranAt": now_iso(),
    }
