"""Impression Generation handler — owner: Chaitra.

v1 returns a deterministic stub draft (no LLM call), but it now STRUCTURES from the report
content (issue #16): the `report` payload is the lean `ris.report.finalized` event
({diagnosticReportId, status, lastUpdatedCursor, ...}) and never carries narrative text inline
(Golden rule 2), so we read the report's `conclusion` from fhir2 by its id and scan it for
critical findings. The fetch is best-effort: if fhir2 is unreachable the draft degrades to "no
acute findings" rather than failing the post-sign safety-net.

Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json

M2: replace the keyword scan with an LLM draft from report + ehrContext + aiFindings + priors.
"""
from __future__ import annotations
import logging
import re

from radagent_common.fhir_client import Fhir2Client
from radagent_common.tracing import now_iso

AGENT_VERSION = "0.1.0"
_log = logging.getLogger(__name__)

# Read-only fhir2 client for report-content lookup (#16). Lazily built so importing this module
# has no side effect; tests/harness override `_FHIR` with a fake.
_FHIR: Fhir2Client | None = None


def _fhir() -> Fhir2Client:
    global _FHIR
    if _FHIR is None:
        _FHIR = Fhir2Client()
    return _FHIR


# Each keyword maps to its own correct clinical label.
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


async def _report_conclusion(report: dict) -> str:
    """The report narrative to structure from. An inline `conclusion` (M2 pre-sign draft, or a
    test) wins; otherwise fetch it from fhir2 by diagnosticReportId. Best-effort: a fhir2 miss or
    error yields "" so the safety-net still returns a valid draft."""
    inline = report.get("conclusion")
    if isinstance(inline, str) and inline.strip():
        return inline
    report_id = report.get("diagnosticReportId")
    if not report_id:
        return ""
    try:
        return await _fhir().get_report_conclusion(report_id) or ""
    except Exception:  # noqa: BLE001 - fhir2 down must not fail the post-sign impression
        _log.warning("fhir2 conclusion fetch failed for %s; drafting without critical detection", report_id)
        return ""


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    conclusion = (await _report_conclusion(report)).lower()

    # Deterministically detect critical findings. Word-boundary match so "mass" does not fire on
    # "massive" (negation like "no pneumothorax" is a known limit; the M2 LLM draft handles it).
    critical_flags = [
        {"label": label, "severity": "critical"}
        for keyword, label in _CRITICAL_KEYWORDS.items()
        if re.search(rf"\b{re.escape(keyword)}\b", conclusion)
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
