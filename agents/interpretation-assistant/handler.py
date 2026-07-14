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

# Referral-reason ICD-10 codes per real-slice tool. A real, narrow signal: the ordering
# clinician already named the suspicion, so the tool can act on it before any pixel-level model
# exists (#27). A code NOT in a tool's set means "nothing to check", not "confirmed normal" -- a
# referral reason that doesn't mention the condition is not imaging evidence against it, so
# absence of a match must stay STUBBED rather than fabricate a negative finding (the same trap the
# #26 COMPLETE-gate guards against on the write side). Lists confirmed with Pranathi (lead review):
#   - pneumothorax-detect: J93.* (spontaneous), S27.0XXA (traumatic), J95.811 (postprocedural,
#     e.g. r/o PTX post-line film).
#   - pe-detect: I26.* (parent + billable children, with/without acute cor pulmonale) and O88.2*
#     (obstetric thromboembolism) -- PE is coded under I26 regardless of cause, so unlike
#     pneumothorax there's no separate traumatic code, but pregnancy/puerperium PE sits outside
#     I26 entirely in the O88.2 family and needs its own entry.
_REASON_CODE_RULES: dict[str, tuple[frozenset[str], str]] = {
    "pneumothorax-detect": (
        frozenset({
            "J93.0", "J93.1", "J93.11", "J93.12", "J93.81", "J93.82", "J93.83", "J93.9",
            "S27.0XXA", "J95.811",
        }),
        "pneumothorax",
    ),
    "pe-detect": (
        frozenset({
            "I26.0", "I26.01", "I26.02", "I26.09",
            "I26.9", "I26.90", "I26.92", "I26.93", "I26.94", "I26.99",
            "O88.2", "O88.211", "O88.212", "O88.213", "O88.219", "O88.22", "O88.23",
        }),
        "pulmonary embolism",
    ),
}


def _reason_finding(tool_id: str, reason_codes: list[str]) -> Optional[dict]:
    codes, condition = _REASON_CODE_RULES[tool_id]
    hit = next((code for code in reason_codes if code.upper() in codes), None)
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
