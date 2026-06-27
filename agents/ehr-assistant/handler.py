"""EHR Assistant handler — owner: Parvati.

v1 = returns an empty-but-valid context packet. M1: fetch from fhir2 via Fhir2Client
(lean-reference: pull from source, never pass PHI in the A2A message).
Input  : { studyContext }
Output : contracts/skills/ehr.schema.json
"""
from __future__ import annotations
from radagent_common.tracing import now_iso
# from radagent_common.fhir_client import Fhir2Client  # M1

AGENT_VERSION = "0.1.0"


async def handle(skill_id: str, payload: dict) -> dict:
    if skill_id != "ehr.assembleContext":
        raise ValueError(f"unexpected skill {skill_id}")
    ctx = payload["studyContext"]
    # TODO(M1): fhir = Fhir2Client(); priors = await fhir.search_imaging_studies(...); etc.
    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "priorStudies": [],
        "relevantLabs": [],
        "activeProblems": [],
        "contrastFlags": {"egfr": None, "priorReaction": False, "onMetformin": False},
        "medicationFlags": {},
        "allergies": [],
        "agentVersion": AGENT_VERSION,
        "assembledAt": now_iso(),
    }
