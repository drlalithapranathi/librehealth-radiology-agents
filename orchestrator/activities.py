"""Temporal activities. ALL network/PHI I/O happens here (never in the workflow)."""
from __future__ import annotations
from typing import Any
from temporalio import activity

from radagent_common.client import call_agent_skill
from radagent_common.fhir_client import Fhir2Client
from . import state


@activity.defn(name=state.ACT_CALL_AGENT)
async def call_agent_skill_activity(agent: str, skill_id: str, payload: dict[str, Any]) -> dict:
    """Invoke an A2A agent skill and return its (contract-validated) JSON output."""
    base_url = state.agent_base_url(agent)
    return await call_agent_skill(base_url, skill_id, payload)


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
async def escalate_activity(workflow_id: str, reason: str) -> None:
    """Human-gate timer fired without a radiologist action -> escalate.

    TODO(M2): real escalation (page on-call, notify lead). For now, log.
    """
    activity.logger.warning("ESCALATE wf=%s reason=%s", workflow_id, reason)


# Convenience for ingress: the RIS poller uses this to find finalized reports.
# Returns (finalized records oldest-first, high-water `_lastUpdated` cursor).
async def poll_finalized_reports(since_iso: str) -> tuple[list[dict], str | None]:
    return await Fhir2Client().poll_finalized_reports(since_iso)
