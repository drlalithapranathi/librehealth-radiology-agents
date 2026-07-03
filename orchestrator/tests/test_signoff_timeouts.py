"""Tier-dependent sign-off gate timeout + on-call paging (#23).

Covers both halves of #23:
- the tier -> timeout mapping (unit) and that the workflow actually uses the tier's timeout
  (integration: a STAT study reaches the sign-off gate, its 1h timer is asserted from history,
  and it escalates on timeout; a failed page does not strand the gate);
- that escalation does real paging (unit): escalate_activity dispatches comms.dispatch marked
  critical, which the Communications Agent routes to the on-call pager.
Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment, ActivityEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import (  # noqa: E402
    StudyWorkflow,
    signoff_timeout_for,
    SIGNOFF_GATE_TIMEOUT_DEFAULT,
)


# --- unit: the tier -> timeout map ------------------------------------------------

def test_timeout_is_tier_dependent_and_ordered():
    stat = signoff_timeout_for("STAT")
    urgent = signoff_timeout_for("URGENT")
    routine = signoff_timeout_for("ROUTINE")
    assert (stat, urgent, routine) == (timedelta(hours=1), timedelta(hours=2), timedelta(hours=4))
    # STAT reads escalate fastest, ROUTINE slowest.
    assert stat < urgent < routine


def test_unknown_or_missing_tier_falls_back_to_default():
    assert SIGNOFF_GATE_TIMEOUT_DEFAULT == timedelta(hours=4)
    for tier in (None, "", "WEIRD"):
        assert signoff_timeout_for(tier) == SIGNOFF_GATE_TIMEOUT_DEFAULT


# --- integration: the workflow uses the tier timeout and escalates ----------------

_ESCALATIONS: list = []


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        # First read needs human review (-> sign-off gate); after escalation, re-verify PASSes.
        _mock_call.n += 1  # type: ignore[attr-defined]
        return {"verificationStatus": "FAIL" if _mock_call.n == 1 else "PASS",  # type: ignore[attr-defined]
                "requiresHumanReview": _mock_call.n == 1, "issues": []}  # type: ignore[attr-defined]
    if skill_id == "triage.score":
        return {"priorityTier": "STAT", "priorityScore": 95}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def _mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None


@activity.defn(name="escalate_activity")
async def _mock_escalate(workflow_id: str, reason: str) -> None:
    _ESCALATIONS.append((workflow_id, reason))


STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_tier",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


def test_stat_study_reaches_signoff_gate_and_escalates():
    """A STAT study reaches the sign-off gate, the tier's 1h timeout fires, and it escalates once.

    Time-skipping fires whatever timer the gate sets, so escalation alone does not prove the
    timeout was tier-specific (1h) rather than the 4h default. We also read the workflow history
    and assert the sign-off wait_condition started a 1h timer -- the direct proof of the tier.
    """
    async def scenario():
        _mock_call.n = 0  # type: ignore[attr-defined]
        _ESCALATIONS.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_escalate]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-tier-stat", task_queue=TASK_QUEUE
                )
                # release the radiologist gate, then let the (STAT = 1h) sign-off gate time out
                for _ in range(200):
                    if await handle.query(StudyWorkflow.current_state) == "AWAITING_RADIOLOGIST":
                        break
                    await asyncio.sleep(0.02)
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()  # env time-skips the STAT sign-off timeout
                # The sign-off gate is the workflow's only timer; assert it used the STAT (1h) tier.
                signoff_timers = [
                    e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
                    async for e in handle.fetch_history_events()
                    if e.HasField("timer_started_event_attributes")
                ]
        assert result["finalState"] == "ARCHIVED"
        assert len(_ESCALATIONS) == 1  # the tier-based sign-off gate timed out and escalated
        assert signoff_timers == [timedelta(hours=1)]  # STAT tier, not the 4h default
    asyncio.run(scenario())


_FAILED_ESCALATE_ATTEMPTS: list = []


@activity.defn(name="escalate_activity")
async def _boom_escalate(workflow_id: str, reason: str) -> dict:
    _FAILED_ESCALATE_ATTEMPTS.append(workflow_id)
    raise RuntimeError("comms down")


def test_escalation_failure_does_not_strand_the_gate():
    """Best-effort paging: if escalate_activity keeps failing, the workflow must NOT be stranded.
    It logs and the verify loop carries on (here the re-verify PASSes, so the study archives)."""
    async def scenario():
        _mock_call.n = 0  # type: ignore[attr-defined]
        _FAILED_ESCALATE_ATTEMPTS.clear()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _boom_escalate]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-tier-boom", task_queue=TASK_QUEUE
                )
                for _ in range(200):
                    if await handle.query(StudyWorkflow.current_state) == "AWAITING_RADIOLOGIST":
                        break
                    await asyncio.sleep(0.02)
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"     # gate not stranded despite the failed paging
        assert _FAILED_ESCALATE_ATTEMPTS               # escalation really was attempted (and failed)
    asyncio.run(scenario())


# --- unit: escalate_activity really pages the on-call via comms.dispatch (#23) -----

def test_escalate_activity_pages_oncall_via_comms(monkeypatch):
    """The escalate activity dispatches `comms.dispatch` marked critical, which the Communications
    Agent routes to the on-call pager. We capture the outbound dispatch and assert it carries the
    workflowId and the critical marker (verificationStatus=FAIL) that trips the pager route
    (agents/communications/handler._is_critical) -- so an escalation actually reaches a human."""
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(base_url=base_url, skill_id=skill_id, payload=payload)
        return {
            "schemaVersion": "1.0.0",
            "workflowId": payload["studyContext"]["workflowId"],
            "dispatchStatus": "SENT",
            "channelResults": [{"channel": "ehr-inbox", "status": "SENT"},
                               {"channel": "oncall-pager", "status": "SENT"}],
            "agentVersion": "0.1.0",
            "dispatchedAt": "2026-07-02T00:00:00Z",
        }

    # escalate_activity resolves call_agent_skill as a module global -> patch it there.
    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)

    result = asyncio.run(ActivityEnvironment().run(
        activities.escalate_activity, "wf_esc", "sign-off gate timed out awaiting radiologist"))

    assert captured["skill_id"] == "comms.dispatch"
    assert captured["payload"]["studyContext"]["workflowId"] == "wf_esc"
    assert captured["payload"]["verification"]["verificationStatus"] == "FAIL"  # -> pager route
    assert "communications" in captured["base_url"]
    assert result["dispatchStatus"] == "SENT"
    assert any(c["channel"] == "oncall-pager" for c in result["channelResults"])
