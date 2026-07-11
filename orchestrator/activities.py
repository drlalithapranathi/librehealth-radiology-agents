"""Temporal activities. ALL network/PHI I/O happens here (never in the workflow)."""
from __future__ import annotations
from typing import Any
from temporalio import activity

import os
from pathlib import Path
from urllib.parse import quote

import yaml

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


def _escalation_policy_path() -> Path:
    """Env override -> the in-repo default (baked into the worker image)."""
    default = Path(__file__).resolve().parent / "config" / "escalation-policy.yaml"
    return Path(os.environ.get("ESCALATION_POLICY_PATH", default))


@activity.defn(name=state.ACT_LOAD_ESCALATION_POLICY)
async def load_escalation_policy_activity(tier: str | None) -> list[dict]:
    """Resolve the sign-off escalation ladder for a priority tier (#29).

    Reads orchestrator/config/escalation-policy.yaml (CI-validated against
    contracts/escalation-policy.schema.json) and returns the tier's ordered rungs; an unknown or
    missing tier gets the policy's defaultTier ladder. Read fresh per gate entry -- no cache --
    so a policy edit (or re-pointed ESCALATION_POLICY_PATH) takes effect without a worker
    restart. Runs as an activity so the workflow stays deterministic: the resolved ladder is
    recorded in history, and a mid-wait policy edit cannot desync a replay.
    """
    with _escalation_policy_path().open() as f:
        policy = yaml.safe_load(f)
    tiers = policy["tiers"]
    ladder = tiers.get(tier or "") or tiers[policy["defaultTier"]]
    levels = ladder["levels"]
    # Validate the two schedule fields the workflow reads with bare subscripts: afterMinutes on
    # every rung, and repeatEveryMinutes on a repeating rung. A parseable-but-malformed policy
    # that omits one would otherwise raise a KeyError inside @workflow.run -- which fails only the
    # workflow TASK and hot-retries forever, wedging the gate with no escalation and no fallback.
    # Surfacing it here as an activity failure routes the gate to its legacy fallback instead (a
    # config disaster must not silence escalation). The in-repo policy is CI-validated against the
    # schema; this guards a live edit or an ESCALATION_POLICY_PATH override, both of which read
    # fresh per gate entry and bypass CI entirely.
    for rung in levels:
        if "afterMinutes" not in rung:
            raise ValueError(f"escalation rung missing afterMinutes: {rung!r}")
        if rung.get("repeat") and "repeatEveryMinutes" not in rung:
            raise ValueError(f"repeating escalation rung missing repeatEveryMinutes: {rung!r}")
    return levels


@activity.defn(name=state.ACT_ESCALATE)
async def escalate_activity(workflow_id: str, reason: str, escalation: dict | None = None) -> dict:
    """The sign-off human gate is still open with no radiologist action -> page a human (#23/#29).

    The orchestrator owns the durable escalation clock (Temporal); each fired ladder rung maps
    onto one Communications Agent dispatch via the A2A `comms.dispatch` boundary. The payload's
    `escalation` slice (contracts/escalation-policy.schema.json $defs/dispatchEscalation) tells
    the agent who to reach (targetRole), how (channels), and how loudly (urgency) -- policy + IDs
    only (lean-reference; no PHI in the message). `escalation=None` is the legacy flat page, kept
    for the workflow's fallback when the policy itself cannot be loaded.

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
    activity.logger.warning(
        "ESCALATE wf=%s level=%s attempt=%s reason=%s",
        workflow_id, (escalation or {}).get("level"), (escalation or {}).get("attempt", 1), reason,
    )
    payload: dict[str, Any] = {"studyContext": {"workflowId": workflow_id}}
    if escalation is None:
        # Legacy flat page: a FAIL verification is the only "page now" lever the pre-#29 payload
        # offered (it trips the on-call pager route in agents/communications handler._is_critical).
        payload["verification"] = {"verificationStatus": "FAIL"}
    else:
        # Pass forward only the dispatch slice of the rung (its scheduling fields stay internal).
        payload["escalation"] = {
            "level": escalation["level"],
            "targetRole": escalation["targetRole"],
            "channels": escalation["channels"],
            "urgency": escalation["urgency"],
            "attempt": escalation.get("attempt", 1),
            "reason": reason,
        }
    return await call_agent_skill(state.agent_base_url("communications"), "comms.dispatch", payload)


# Convenience for ingress: the RIS poller uses this to find finalized reports.
# Returns (finalized records oldest-first, high-water `_lastUpdated` cursor).
async def poll_finalized_reports(since_iso: str) -> tuple[list[dict], str | None]:
    return await Fhir2Client().poll_finalized_reports(since_iso)
