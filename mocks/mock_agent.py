"""Generic A2A mock agent — stands in for a not-yet-built agent during M1 integration.

Serves any card from /contracts/cards and returns a canned, contract-valid response for
its skill. Run, e.g.:
    python mocks/mock_agent.py worklist-triage 8201
"""
from __future__ import annotations
import sys
import uvicorn
from radagent_common.tracing import now_iso
from radagent_common.a2a import build_agent_app

_CANNED = {
    "triage.score": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf, "priorityScore": 50,
                                "priorityTier": "ROUTINE", "rationale": ["mock"], "agentVersion": "mock",
                                "computedAt": now_iso()},
    "ehr.assembleContext": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf, "priorStudies": [],
                                       "relevantLabs": [], "activeProblems": [], "contrastFlags": {},
                                       "medicationFlags": {}, "allergies": [], "agentVersion": "mock",
                                       "assembledAt": now_iso()},
    "interpretation.runTools": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf, "toolsSelected": [],
                                           "findings": [], "overallStatus": "STUBBED", "agentVersion": "mock",
                                           "ranAt": now_iso()},
    "impression.generate": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf,
                                       "impressionText": "[mock]", "structuredFindings": [],
                                       "recommendations": [], "criticalFlags": [], "agentVersion": "mock",
                                       "generatedAt": now_iso()},
    "report.verify": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf, "verificationStatus": "PASS",
                                 "requiresHumanReview": False, "issues": [], "agentVersion": "mock",
                                 "verifiedAt": now_iso()},
    "comms.dispatch": lambda wf: {"schemaVersion": "1.0.0", "workflowId": wf,
                                  "dispatchStatus": "SENT", "channelResults": [],
                                  "agentVersion": "mock", "dispatchedAt": now_iso()},
}


async def handle(skill_id: str, payload: dict) -> dict:
    wf = payload.get("studyContext", {}).get("workflowId", "wf_mock")
    if skill_id not in _CANNED:
        raise ValueError(f"Unknown skill: {skill_id}")
    return _CANNED[skill_id](wf)


if __name__ == "__main__":
    agent_dir = sys.argv[1] if len(sys.argv) > 1 else "worklist-triage"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8201
    uvicorn.run(build_agent_app(agent_dir, handle).build(), host="0.0.0.0", port=port)
