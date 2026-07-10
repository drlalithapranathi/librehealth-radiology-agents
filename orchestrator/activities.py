"""Temporal activities. ALL network/PHI I/O happens here (never in the workflow)."""
from __future__ import annotations
from typing import Any
from temporalio import activity

import os
from urllib.parse import quote

from radagent_common.client import call_agent_skill, start_agent_skill
from radagent_common.fhir_client import Fhir2Client
from . import state


@activity.defn(name=state.ACT_CALL_AGENT)
async def call_agent_skill_activity(agent: str, skill_id: str, payload: dict[str, Any]) -> dict:
    """Invoke an A2A agent skill and return its (contract-validated) JSON output."""
    base_url = state.agent_base_url(agent)
    return await call_agent_skill(base_url, skill_id, payload)


@activity.defn(name=state.ACT_START_AGENT)
async def start_agent_skill_activity(agent: str, skill_id: str, payload: dict[str, Any],
                                     workflow_id: str) -> str:
    """Start a skill in push-notification mode and return its A2A taskId (#24).

    The agent POSTs the result to this ingress (/callbacks/a2a/<workflowId>), which relays it to
    the workflow as a `skill_completed` signal — the workflow correlates on the returned taskId.
    The shared A2A_CALLBACK_TOKEN (env) authenticates the callback; the callback URL carries the
    workflowId (so ingress needs no task->workflow index) and the skillId (so ingress can
    re-validate the delivered result against its contract before relaying it)."""
    base_url = state.agent_base_url(agent)
    callback_url = (f"{state.callback_base_url()}/callbacks/a2a/{workflow_id}"
                    f"?skill={quote(skill_id)}")
    return await start_agent_skill(
        base_url, skill_id, payload,
        callback_url=callback_url,
        callback_token=os.environ.get("A2A_CALLBACK_TOKEN", ""),
    )


@activity.defn(name=state.ACT_PUBLISH_PRIORITY)
async def publish_priority_activity(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    """Make the triage priority visible to the Worklist API (orchestrator = source of truth).

    TODO(M1): write to the Worklist API / priority store. No DICOM tag mutation.
    """
    activity.logger.info(
        "publish priority wf=%s study=%s tier=%s score=%s",
        workflow_id, study_instance_uid, triage.get("priorityTier"), triage.get("priorityScore"),
    )


@activity.defn(name=state.ACT_ESCALATE)
async def escalate_activity(workflow_id: str, reason: str) -> dict:
    """Sign-off human-gate timed out with no radiologist action -> page the on-call (#23).

    The orchestrator owns the durable escalation clock (Temporal), so on timeout it invokes the
    Communications Agent exactly once via the A2A `comms.dispatch` boundary. We mark the dispatch
    critical so the agent routes it to the on-call pager channel -- an unsigned, flagged read must
    reach a human. The page carries IDs only (lean-reference; no PHI in the message).

    TODO(M3): wire the REAL Communications Agent (CritCom). This dispatch targets the in-repo
    comms.dispatch STUB; CritCom is shaped differently and needs an adapter:
      1. protocol: A2A `message/send` + `X-API-Key` with a natural-language instruction, not this
         structured `comms.dispatch` skill;
      2. identifiers: a real FHIR ref (DiagnosticReport / ServiceRequest / DICOM accession),
         resolved from the study (#11) -- `workflowId` is meaningless to CritCom;
      3. context/creds: FHIR endpoint + token as A2A metadata, plus a Gemini/Vertex key;
      4. reply: parse CritCom's Task/free-text result back into dispatchStatus/channelResults.
    Note: CritCom's own gate is 'ordering physician didn't ACK a critical result', a DIFFERENT gate
    from this 'radiologist didn't SIGN' one -- when wiring CritCom, don't double-page.
    """
    activity.logger.warning("ESCALATE wf=%s reason=%s", workflow_id, reason)
    payload = {
        "studyContext": {"workflowId": workflow_id},
        # Mark this dispatch critical so the agent adds the on-call pager channel. This is the only
        # lever the current comms.dispatch contract offers for "page now"; an explicit urgency
        # field is a possible contract follow-up (see the M3 CritCom adapter).
        "verification": {"verificationStatus": "FAIL"},
    }
    return await call_agent_skill(state.agent_base_url("communications"), "comms.dispatch", payload)


# Convenience for ingress: the RIS poller uses this to find finalized reports.
# Returns (finalized records oldest-first, high-water `_lastUpdated` cursor).
async def poll_finalized_reports(since_iso: str) -> tuple[list[dict], str | None]:
    return await Fhir2Client().poll_finalized_reports(since_iso)
