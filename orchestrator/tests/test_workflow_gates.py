"""Human-gate durability tests for StudyWorkflow (issue #6; gate ladder-wired by #29).

Uses Temporal's time-skipping test environment to prove the two risk items in #6:
  * ESCALATION — the sign-off gate holds while the report sits unsigned, climbing this ROUTINE
    study's escalation ladder (rung 1 at 4h, rung 2 at 8h, then repeats) until the ack releases it;
  * RESTART-DURING-WAIT — a workflow blocked at AWAITING_RADIOLOGIST survives a worker restart
    and completes once signalled.
Plus the happy path: report_finalized releases the radiologist gate and current_state tracks it.

Activities are mocked (registered under the same names the workflow calls); the multi-hour rung
timers are advanced with env.sleep(). Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

# Compact ROUTINE-shaped ladder (mirrors the real policy's structure: widening audience,
# repeating final rung) served by the mocked policy activity.
_LADDER = [
    {"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
     "channels": ["in-app", "email"], "urgency": "routine"},
    {"level": 2, "afterMinutes": 480, "targetRole": "on-call-radiologist",
     "channels": ["email", "sms"], "urgency": "urgent", "repeat": True, "repeatEveryMinutes": 120},
]

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_gate_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc123", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

# Shared control/record state for the mock activities (reset per scenario).
_STATE: dict = {}


def _reset(verify_plan: list[tuple[str, bool]]) -> None:
    _STATE.clear()
    _STATE["verify_plan"] = list(verify_plan)   # per-call (verificationStatus, requiresHumanReview)
    _STATE["verify_i"] = 0
    _STATE["escalations"] = []


@activity.defn(name="call_agent_skill_activity")
async def mock_call_agent(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        plan = _STATE["verify_plan"]
        status, human = plan[min(_STATE["verify_i"], len(plan) - 1)]
        _STATE["verify_i"] += 1
        return {"verificationStatus": status, "requiresHumanReview": human, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None


@activity.defn(name="escalate_activity")
async def mock_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> None:
    _STATE["escalations"].append((workflow_id, reason, escalation))


@activity.defn(name="load_escalation_policy_activity")
async def mock_load_policy(tier: str | None) -> list[dict]:
    return _LADDER


def _worker(env: WorkflowEnvironment) -> Worker:
    # max_cached_workflows=0 disables the sticky-queue cache: every workflow task replays from
    # history (what a real worker restart does), and it avoids the sticky schedule-to-start
    # timeout that the time-skipping server won't auto-advance across a worker restart.
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=[mock_call_agent, mock_publish, mock_escalate, mock_load_policy],
        max_cached_workflows=0,
    )


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


def test_report_finalized_releases_radiologist_gate_and_archives():
    """Happy path: the report_finalized signal releases AWAITING_RADIOLOGIST -> ARCHIVED, no escalation."""
    async def scenario():
        _reset([("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-signal", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")  # genuinely blocked at the gate
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        assert _STATE["escalations"] == []
    asyncio.run(scenario())


async def _wait_escalations(count: int, tries: int = 600) -> None:
    """Real-time poll until `count` escalations landed (activity runs shortly after its timer)."""
    for _ in range(tries):
        if len(_STATE["escalations"]) >= count:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never saw {count} escalations (got {len(_STATE['escalations'])})")


def test_unsigned_report_climbs_ladder_until_ack_releases_the_gate():
    """Escalation (#29): the sign-off gate holds while unsigned, climbing the ladder — rung 1,
    rung 2, then the repeating final rung — and the ack releases it; the loop re-verifies to PASS.

    Time skipping is locked except inside env.sleep()/result-await, so each advance fires
    exactly the rung it targets and the escalation sequence is deterministic.
    """
    async def scenario():
        # First verify needs human review (-> sign-off gate); after the ack, re-verify PASSes.
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-escalate", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await env.sleep(timedelta(minutes=241))   # past rung 1 (240m)
                await _wait_escalations(1)
                await env.sleep(timedelta(minutes=241))   # past rung 2 (480m from entry)
                await _wait_escalations(2)
                await env.sleep(timedelta(minutes=121))   # past the repeat cadence (120m)
                await _wait_escalations(3)
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        rungs = [esc for (_, _, esc) in _STATE["escalations"]]
        assert [r["level"] for r in rungs] == [1, 2, 2]                 # ladder, then the repeat
        assert [r["targetRole"] for r in rungs] == [
            "reading-radiologist", "on-call-radiologist", "on-call-radiologist"]  # widening audience
        assert rungs[2]["attempt"] == 2                                 # the re-fire is marked
    asyncio.run(scenario())


# A single fast-repeating rung so one time-skip can drive the whole repeat cadence to the cap.
_REPEAT_LADDER = [
    {"level": 1, "afterMinutes": 1, "targetRole": "reading-radiologist",
     "channels": ["in-app"], "urgency": "routine", "repeat": True, "repeatEveryMinutes": 1},
]


@activity.defn(name="load_escalation_policy_activity")
async def mock_load_repeat_policy(tier: str | None) -> list[dict]:
    return _REPEAT_LADDER


def test_repeating_rung_stops_at_the_cap_and_the_ack_still_opens_the_gate():
    """Backstop (#29): a repeating final rung re-fires exactly ESCALATION_REPEAT_CAP times (a
    history-growth guard), then the gate holds with no further paging and the ack still releases
    it. A 1-minute cadence lets one time-skip fire the whole ladder+repeat sequence; the exact
    count and the post-cap hold are what a regression in the loop bound or the terminal wait breaks.
    """
    from orchestrator.workflow import ESCALATION_REPEAT_CAP

    async def scenario():
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[mock_call_agent, mock_publish, mock_escalate,
                                          mock_load_repeat_policy], max_cached_workflows=0):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-cap", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                # One skip past the last re-fire (~cap minutes from entry) fires every rung.
                await env.sleep(timedelta(minutes=ESCALATION_REPEAT_CAP + 30))
                await _wait_escalations(ESCALATION_REPEAT_CAP)
                # Cap reached: still parked at the gate (terminal hold), no (cap+1)th page fired.
                assert await handle.query(StudyWorkflow.current_state) == "AWAITING_SIGNOFF"
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        rungs = [esc for (_, _, esc) in _STATE["escalations"]]
        assert len(rungs) == ESCALATION_REPEAT_CAP            # exactly the cap, not one more
        assert rungs[-1]["attempt"] == ESCALATION_REPEAT_CAP  # the final re-fire is the cap-th
    asyncio.run(scenario())


def test_workflow_survives_worker_restart_during_gate():
    """Restart-during-wait: kill the worker while the workflow waits at the gate; a fresh worker
    resumes it from durable history and it completes once signalled."""
    async def scenario():
        _reset([("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):  # worker #1
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-restart", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
            # worker #1 is now STOPPED (context exited) — the workflow is still parked at the gate.
            async with _worker(env):  # worker #2 (the "restart")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"  # resumed on the new worker
    asyncio.run(scenario())
