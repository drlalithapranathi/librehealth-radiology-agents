"""Human-gate durability tests for StudyWorkflow (issue #6).

Uses Temporal's time-skipping test environment to prove the two risk items in #6:
  * ESCALATION — the sign-off gate times out (the tier timeout; this ROUTINE study = 4h) and fires escalate_activity;
  * RESTART-DURING-WAIT — a workflow blocked at AWAITING_RADIOLOGIST survives a worker restart
    and completes once signalled.
Plus the happy path: report_finalized releases the radiologist gate and current_state tracks it.

Activities are mocked (registered under the same names the workflow calls); the 4h timer is
time-skipped. Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

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
async def mock_escalate(workflow_id: str, reason: str) -> None:
    _STATE["escalations"].append((workflow_id, reason))


def _worker(env: WorkflowEnvironment) -> Worker:
    # max_cached_workflows=0 disables the sticky-queue cache: every workflow task replays from
    # history (what a real worker restart does), and it avoids the sticky schedule-to-start
    # timeout that the time-skipping server won't auto-advance across a worker restart.
    return Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[StudyWorkflow],
        activities=[mock_call_agent, mock_publish, mock_escalate],
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


def test_signoff_timeout_escalates_then_completes():
    """Escalation: the sign-off gate times out -> escalate_activity fires; the loop re-verifies to PASS."""
    async def scenario():
        # First verify needs human review (-> sign-off gate); after the timeout+escalate, re-verify PASSes.
        _reset([("FAIL", True), ("PASS", False)])
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with _worker(env):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-gate-escalate", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized, {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()  # env time-skips the 4h sign-off gate
        assert result["finalState"] == "ARCHIVED"
        assert len(_STATE["escalations"]) == 1  # the timed-out gate escalated exactly once
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
