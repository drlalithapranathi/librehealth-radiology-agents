"""Impression Generation handler — owner: Chaitra.

v1 returns a deterministic stub draft (no LLM call), but it now STRUCTURES from the report
content (issue #16): the `report` payload is the lean `ris.report.finalized` event
({diagnosticReportId, status, lastUpdatedCursor, ...}) and never carries narrative text inline
(Golden rule 2), so we read the report's `conclusion` from fhir2 by its id and scan it for
critical findings. The fetch is best-effort: if fhir2 is unreachable the draft degrades to "no
acute findings" rather than failing the post-sign safety-net.

Pre-sign (#26): before a report exists, `report` is omitted/empty, so the only signal available
is `aiFindings` (contracts/skills/interpretation.schema.json). Each COMPLETE finding's `label`
runs through the same negation-aware critical-term scan as the report conclusion — but every
signal is scanned SEPARATELY (the conclusion's finding-bearing sections, then each label on its
own, #78), so a pertinent negative in one signal can never silence a positive in another.
STUBBED/ERROR labels are excluded: a STUBBED label may describe a NEGATIVE screen or a referral
code (not a model-asserted finding), and an ERROR label describes a failure -- only COMPLETE
labels assert findings. Post-sign, an aiFindings hit still surfaces even if the conclusion
text misses it. This keeps the handler's own I/O timing-agnostic; wiring the orchestrator to
actually call this skill pre-sign and write the draft back into the RIS is orchestrator/shared-lib
work tracked separately on #26 (out of scope here).

Input  : { studyContext, report?, ehrContext?, aiFindings? }
Output : contracts/skills/impression.schema.json

M2: replace the keyword scan with an LLM draft from report + ehrContext + aiFindings + priors.
"""
from __future__ import annotations
import logging

from radagent_common.fhir_client import Fhir2Client
from radagent_common.negation import find_asserted_terms, scannable_text
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


def _complete_finding_labels(ai_findings: dict) -> list[str]:
    """Labels of COMPLETE findings (#26), one entry per finding. STUBBED/ERROR labels are excluded:
    a STUBBED label may describe a NEGATIVE screen or a referral code (not a model-asserted finding)
    and an ERROR label a failure -- only COMPLETE labels assert findings. A LIST, not a joined
    string: each label is scanned on its own, so a negation cue in one tool's label
    ("No hemorrhage") can never bleed across and silence a positive term in the next tool's (#78)."""
    return [
        finding.get("label") or ""
        for finding in ai_findings.get("findings", [])
        if finding.get("status") == "COMPLETE"
    ]


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "impression.generate":
        raise ValueError(f"unexpected skill {skill_id}")

    ctx = payload["studyContext"]
    report = payload.get("report") or {}
    ai_findings = payload.get("aiFindings") or {}
    conclusion = await _report_conclusion(report)

    # Deterministically detect critical findings from whichever signal is available (pre-sign:
    # aiFindings only; post-sign: report conclusion, plus aiFindings if also passed forward).
    # Negation-aware (#78): a real NORMAL report is pertinent negatives ("No pneumothorax, effusion,
    # or consolidation"), and a bare keyword match flagged every one of them, FAILing verification
    # and parking the whole normal cohort at the sign-off gate. Three scan rules, each of which a
    # reproduced false flag or false silence forced:
    #   * only the finding-bearing SECTIONS of the narrative are scanned (scannable_text): the
    #     INDICATION names the suspicion ("evaluate for pneumothorax"), not a finding;
    #   * every signal is scanned SEPARATELY -- the conclusion and EACH finding label -- so a
    #     negation in one can never bleed across and silence a positive in another;
    #   * word-boundary match, so "mass" never fires on "massive".
    scan_texts = [scannable_text(conclusion)] + _complete_finding_labels(ai_findings)
    hits: set[str] = set()
    for text in scan_texts:
        hits |= set(find_asserted_terms(text, _CRITICAL_KEYWORDS))
    critical_flags = [
        {"label": label, "severity": "critical"}
        for keyword, label in _CRITICAL_KEYWORDS.items()
        if keyword in hits
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
