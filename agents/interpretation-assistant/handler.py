"""Interpretation Assistant handler — owner: Chaitra.

v1 = tool REGISTRY that selects by modality/study-type and returns STUBBED results.
M3: wire real CAD/detection tools behind the same registry interface. First real slice (#27):
pneumothorax-detect cross-checks the referral reason rather than image pixels -- there is no
pixel-read path (or imaging deps) yet, so this is a genuine but narrow interim signal, not a CAD
model. Every other tool stays STUBBED until it gets its own real implementation.
Input  : { studyContext }
Output : contracts/skills/interpretation.schema.json
"""
from __future__ import annotations
from typing import Optional
from radagent_common.tracing import now_iso
from registry import select_tools

AGENT_VERSION = "0.2.0"

# ICD-10 codes that code the referral reason itself as pneumothorax. A real, narrow signal: the
# ordering clinician already named the suspicion, so the tool can act on it before any pixel-level
# model exists (#27). A code NOT in this set means "nothing to check", not "confirmed normal" -- a
# referral reason that doesn't mention pneumothorax is not imaging evidence against it, so absence
# of a match must stay STUBBED rather than fabricate a negative finding (the same trap the #26
# COMPLETE-gate guards against on the write side).
_PNEUMOTHORAX_REASON_CODES = {
    "J93.0", "J93.1", "J93.11", "J93.12", "J93.81", "J93.82", "J93.83", "J93.9", "S27.0XXA",
}


def _pneumothorax_reason_finding(reason_codes: list[str]) -> Optional[dict]:
    hit = next((code for code in reason_codes if code.upper() in _PNEUMOTHORAX_REASON_CODES), None)
    if hit is None:
        return None
    return {
        "toolId": "pneumothorax-detect",
        "label": f"Referral reason coded {hit} (pneumothorax); imaging-based detection pending M3 pixel analysis",
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
        # evidence for pneumothorax any more than a non-matching code is evidence against it (see
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
        real = _pneumothorax_reason_finding(reason_codes) if tool == "pneumothorax-detect" else None
        findings.append(real or {
            "toolId": tool, "label": "", "confidence": None, "evidenceRef": None, "status": "STUBBED",
        })

    tools_selected = [
        {"toolId": f["toolId"], "version": "stub-0" if f["status"] == "STUBBED" else "referral-rule-1",
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
