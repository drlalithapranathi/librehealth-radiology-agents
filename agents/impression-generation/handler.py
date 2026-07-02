"""Impression Generation handler — owner: Chaitra.

Consumes the finalised report and populates structuredFindings/criticalFlags
deterministically (no LLM call in v1).

Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"

# Each keyword maps to its own correct clinical label
_CRITICAL_KEYWORDS: dict[str, str] = {
    "dissection":  "aortic dissection",
    "pneumothorax": "pneumothorax",
    "hemorrhage":  "intracranial hemorrhage",
    "occlusion":   "vascular occlusion",
    "rupture":     "rupture",
    "infarct":     "infarction",
    "embolism":    "pulmonary embolism",
    "fracture":    "fracture",
    "mass":        "mass lesion",
    "tumor":       "neoplasm",
}


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    findings_text = (report.get("findingsText") or "").lower()

    # Deterministically detect critical findings
    critical_flags = [
        {"label": label, "severity": "critical"}
        for keyword, label in _CRITICAL_KEYWORDS.items()
        if keyword in findings_text
    ]

    structured_findings = [
        {"label": flag["label"], "severity": flag["severity"]}
        for flag in critical_flags
    ]

    if critical_flags:
        impression = (
            f"Findings are consistent with {critical_flags[0]['label']}. "
            "Urgent clinical correlation and appropriate follow-up recommended."
        )
    else:
        impression = "No acute findings identified. Clinical correlation recommended."

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "impressionText": impression,
        "structuredFindings": structured_findings,
        "recommendations": [
            {"text": "Urgent clinical consultation recommended."}
            if critical_flags else
            {"text": "Routine follow-up as clinically indicated."}
        ],
        "criticalFlags": critical_flags,
        "agentVersion": AGENT_VERSION,
        "generatedAt": now_iso(),
    }