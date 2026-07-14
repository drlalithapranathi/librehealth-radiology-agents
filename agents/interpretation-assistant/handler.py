"""Interpretation Assistant handler — owner: Chaitra.

The tool REGISTRY selects by modality/study-type; each selected tool then reports at one of three
levels of reality, and the level is visible in the output rather than implied:

  * PIXELS (#27) -- `cxr-screen` runs a real pretrained classifier over the study's image data
    (cxr_model.py). This is the first tool in the system that actually looks at the image. It
    reports COMPLETE, with a real confidence, and an evidenceRef naming the instance it scored.
  * REFERRAL REASON (#27) -- `pneumothorax-detect` and `pe-detect` cross-check order.reasonCode
    rather than pixels. A genuine but narrow interim signal, not a CAD model, so they stay STUBBED.
  * STUBBED -- everything else, until it gets its own real implementation.

A tool that cannot run degrades to STUBBED (or ERROR) and NEVER invents a negative: "nothing found"
from a tool that never looked is the automation-bias trap the #26 COMPLETE-gate exists to prevent.

Input  : { studyContext }
Output : contracts/skills/interpretation.schema.json
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from radagent_common.tracing import now_iso
from registry import select_tools

log = logging.getLogger(__name__)

AGENT_VERSION = "0.4.0"

# Is this image built with the pixel/model extras? Decided ONCE, at import, by whether the imports
# succeed -- not discovered over the network mid-study. cxr_model imports torch eagerly for exactly
# this reason. The agent-tests CI lane installs neither extra, so PIXEL_TOOLING is False there and
# cxr-screen stays STUBBED, which is why the pre-#27 suite still passes untouched.
#
# These are module-level names rather than function-local imports so a test can substitute a fake
# Orthanc and a fake model and exercise the pixel path WITHOUT torch. A seam that only exists in
# the presence of a 1.5GB dependency is a seam nobody tests.
try:
    from radagent_common.imaging import NotAnImage, dicom_to_greyscale
    from radagent_common.orthanc_client import OrthancClient

    from cxr_model import score, summarise

    PIXEL_TOOLING = True
except ImportError as _exc:  # pragma: no cover - covered by the import-guard test
    log.info("interpretation: pixel/model extras absent (%s); pixel tools stay STUBBED", _exc)
    PIXEL_TOOLING = False

    class NotAnImage(Exception):  # type: ignore[no-redef]
        """Placeholder so the except-clause below is always a valid type."""

    OrthancClient = dicom_to_greyscale = score = summarise = None  # type: ignore[assignment]

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


_MODEL_VERSION = "cxr-densenet121-res224-all"


def _tool_version(finding: dict) -> str:
    """What actually produced this finding. Visible in toolsSelected[].version so a consumer -- and
    anyone auditing why a chart says what it says -- can tell a real model from a referral-code rule
    from a stub. Three different things must not all report as "stub-0".

    A cxr-screen that DEGRADED to STUBBED (extras absent, no instances) reports "stub-0", not the
    model version: claiming a model that never ran is the same lie as inventing a finding.
    """
    if finding["toolId"] in _PIXEL_TOOLS and finding["status"] in ("COMPLETE", "ERROR"):
        return _MODEL_VERSION
    if finding["evidenceRef"]:
        return "referral-rule-1"
    return "stub-0"


def _overall_status(statuses: list[str]) -> str:
    unique = set(statuses)
    if not unique or unique == {"STUBBED"}:
        return "STUBBED"
    if unique == {"COMPLETE"}:
        return "COMPLETE"
    if unique == {"ERROR"}:
        return "ERROR"
    return "PARTIAL"


# Tools that read PIXELS. Everything else in the registry either cross-checks the referral reason
# (above) or is still a stub. cxr-screen is the first real model in the system (#27).
_PIXEL_TOOLS = frozenset({"cxr-screen"})


async def _pixel_finding(tool_id: str, ctx: dict) -> Optional[dict]:
    """Run a real model over the study's pixels, or return None to leave the tool STUBBED.

    DEGRADES, NEVER CRASHES. Three ways this legitimately does not run, and none of them may take
    the study down -- interpretation is one leg of a pre-read fan-out, and a study that cannot be
    screened still has to reach the radiologist:
      * the imaging/model extras are not installed (the default agent-tests lane installs neither,
        which is what keeps 65 existing tests green and torch out of CI) -> STUBBED;
      * Orthanc has no instances for the study, or the instance carries no pixels -> STUBBED;
      * the model itself throws -> ERROR, reported honestly.

    What it must never do is invent a negative. A tool that cannot look at the image and reports
    "nothing found" is the automation-bias trap the #26 COMPLETE-gate exists to prevent, and it is
    worse here than in the stub, because this one carries a model's authority.
    """
    if tool_id not in _PIXEL_TOOLS or not PIXEL_TOOLING:
        return None

    orthanc_study_id = (ctx.get("study") or {}).get("orthancStudyId")
    if not orthanc_study_id:
        return None

    try:
        client = OrthancClient()
        instances = await client.list_study_instances(orthanc_study_id)
        if not instances:
            log.warning("cxr-screen: study %s has no instances", orthanc_study_id)
            return None

        # The FIRST instance, in (SeriesNumber, InstanceNumber) order -- the frontal view of a
        # frontal+lateral study. list_study_instances guarantees that order; an arbitrary pick
        # would score the lateral on some studies and the frontal on others.
        instance_id = instances[0]
        pixels = dicom_to_greyscale(await client.get_instance_dicom(instance_id))

        # Inference is CPU-bound and blocking; keep it off the event loop so one study being
        # screened does not stall every other A2A request this agent is serving.
        probs = await asyncio.to_thread(score, pixels)
    except NotAnImage as exc:
        log.warning("cxr-screen: %s carries no pixel data (%s)", orthanc_study_id, exc)
        return None
    except Exception as exc:  # model/transport failure -> ERROR, not a fabricated negative
        log.exception("cxr-screen failed for %s", orthanc_study_id)
        return {
            "toolId": tool_id,
            "label": f"screening model did not run: {type(exc).__name__}",
            "confidence": None,
            "evidenceRef": None,
            "status": "ERROR",
        }

    label, confidence = summarise(probs)
    return {
        "toolId": tool_id,
        "label": label,
        "confidence": confidence,
        # Text locator, same convention as the referral-reason slices: the instance the model
        # actually scored, so a reader can pull up the exact frame. Not a DICOM SC/overlay ref --
        # writing AI-made images into the record is deferred (#59) and needs its own safety review.
        "evidenceRef": f"orthanc:instance/{instance_id}",
        "status": "COMPLETE",
    }


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
        real = await _pixel_finding(tool, ctx)
        if real is None and tool in _REASON_CODE_RULES:
            real = _reason_finding(tool, reason_codes)
        findings.append(real or {
            "toolId": tool, "label": "", "confidence": None, "evidenceRef": None, "status": "STUBBED",
        })

    tools_selected = [
        {"toolId": f["toolId"], "version": _tool_version(f), "status": f["status"]}
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
