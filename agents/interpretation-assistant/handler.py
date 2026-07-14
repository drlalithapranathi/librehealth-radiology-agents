"""Interpretation Assistant handler — owner: Chaitra.

v1 = tool REGISTRY that selects by modality/study-type and returns STUBBED results.
M3: wire real CAD/detection tools behind the same registry interface. Real slices so far (#27):
pneumothorax-detect and pe-detect each cross-check the referral reason rather than image pixels --
there is no pixel-read path (or imaging deps) yet, so this is a genuine but narrow interim signal,
not a CAD model. Both run through the same table-driven _reason_finding rule rather than one
hand-copied function per tool (per lead review). Every other tool stays STUBBED until it gets its
own real implementation.
Input  : { studyContext }
Output : contracts/skills/interpretation.schema.json
"""
from __future__ import annotations
from typing import Optional
from radagent_common.tracing import now_iso
from registry import select_tools

AGENT_VERSION = "0.3.0"

# Referral-reason ICD-10 codes per real-slice tool, matched by FAMILY PREFIX rather than exact
# code. worklist-triage normalises the same order.reasonCode field to a 3-char ICD-10 category
# (agents/worklist-triage/handler.py:_reason_code_signals), so an exact-string list here silently
# disagreed with triage on the same order: triage escalated "I26" or "I2699" as urgent PE while
# this tool stayed silent on the identical code (#27 follow-up, Saptarshi/Pranathi). Prefixes
# confirmed with Pranathi (lead review):
#   - pneumothorax-detect: "J93" (spontaneous pneumothorax + air leak -- the whole family is
#     pneumothorax, so the prefix can't over-match). S27.0XXA (traumatic) and J95.811
#     (postprocedural, e.g. r/o PTX post-line film) stay explicit full codes because their
#     families -- S27 intrathoracic injury generally, J95 postprocedural respiratory
#     complications generally -- are NOT all pneumothorax.
#   - pe-detect: "I26" (parent + all billable children, with/without acute cor pulmonale --
#     all of I26 is pulmonary embolism, so the prefix can't over-match). "O882" (dot-normalised
#     O88.2, obstetric thromboembolism) stays a 4-char prefix rather than the 3-char "O88"
#     family, because O88 also covers air/amniotic-fluid/septic embolism, which are not PE.
_REASON_CODE_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "pneumothorax-detect": (("J93", "S270XXA", "J95811"), "pneumothorax"),
    "pe-detect": (("I26", "O882"), "pulmonary embolism"),
}


def _normalize_reason_code(code: str) -> str:
    """Same normalisation shape as worklist-triage's _reason_code_signals (dot-stripped,
    upper), so both agents read order.reasonCode the same way."""
    return code.upper().replace(".", "")


def _reason_finding(tool_id: str, reason_codes: list[str]) -> Optional[dict]:
    prefixes, condition = _REASON_CODE_RULES[tool_id]
    hit = next((code for code in reason_codes if _normalize_reason_code(code).startswith(prefixes)), None)
    if hit is None:
        return None
    return {
        "toolId": tool_id,
        "label": f"Referral reason coded {hit} ({condition}); imaging-based detection pending M3 pixel analysis",
        "confidence": None,
        # Text pointer to where the evidence lives, not an image-region ref: no pixel read exists
        # yet, and writing a DICOM SC/overlay into Orthanc needs a safety review we haven't done
        # (#27). evidenceRef is `["string", "null"]` in the contract, so a plain-text locator is
        # a legitimate value here, not a placeholder for the M3 image-based ref.
        "evidenceRef": f"order.reasonCode={hit}",
        # `status` stays STUBBED even though label/evidenceRef are populated: COMPLETE is reserved
        # for real pixel-level results, because it gates the pre-sign fhir2 write
        # (orchestrator/workflow.py:_has_complete_finding -> _presign_impression, before
        # AWAITING_RADIOLOGIST). A referral reason the ordering clinician typed is not imaging
        # evidence for the condition any more than a non-matching code is evidence against it (see
        # the comment above on absence-of-match) -- so it must not trip a pre-read critical-finding
        # chart write. Do not flip this to COMPLETE without also addressing the fhir2 write-back
        # security/PHI review (#30).
        "status": "STUBBED",
    }


def _overall_status(statuses: list[str]) -> str:
    unique = set(statuses)
    if not unique or unique == {"STUBBED"}:
        return "STUBBED"
    if unique == {"COMPLETE"}:
        return "COMPLETE"
    if unique == {"ERROR"}:
        return "ERROR"
    return "PARTIAL"


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "interpretation.runTools":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    modality = ctx["study"].get("modality", "")
    desc = ctx["study"].get("studyDescription", "")
    reason_codes = ctx.get("order", {}).get("reasonCode") or []
    tools = select_tools(modality, desc)

    findings = []
    for tool in tools:
        real = _reason_finding(tool, reason_codes) if tool in _REASON_CODE_RULES else None
        findings.append(real or {
            "toolId": tool, "label": "", "confidence": None, "evidenceRef": None, "status": "STUBBED",
        })

    tools_selected = [
        {"toolId": f["toolId"], "version": "referral-rule-1" if f["evidenceRef"] else "stub-0",
         "status": f["status"]}
        for f in findings
    ]

    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "toolsSelected": tools_selected,
        "findings": findings,
        "overallStatus": _overall_status([f["status"] for f in findings]),
        "agentVersion": AGENT_VERSION,
        "ranAt": now_iso(),
    }
