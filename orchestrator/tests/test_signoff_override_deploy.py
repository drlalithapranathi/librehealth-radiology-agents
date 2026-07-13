"""#57: the override must release the studies that are ALREADY stranded -- not just future ones.

Every other test in this suite starts a FRESH workflow, and a fresh workflow is always patched, so it
always takes the new path. That is precisely the population #56 is NOT about. The studies #56 filed
are the ones parked at the sign-off gate RIGHT NOW, on pre-#57 code, with no patch marker in their
history. A worker running #57 inherits them: it REPLAYS their history, then continues live.

`workflow.patched()` is memoized per execution and returns False while replaying. So a marker read on
a REPLAYED line -- e.g. at gate entry, which every parked study re-executes -- pins that study to the
pre-#57 branch forever. The override then releases the gate, the study re-verifies the unchanged
report, re-FAILs, and re-parks: #56 exactly, now answered with a 202. The whole MR would have been
inert for the only studies it was written for.

So the marker is read lazily, after the awaits (see _hold_signoff_gate's docstring). This test is the
guard, and it is a real one: it drives the actual deploy. Worker A runs the frozen pre-#57
StudyWorkflow (fixtures/pre57_workflow.py) and parks a study at the gate; worker A goes away; worker B
runs the real #57 StudyWorkflow and inherits the same execution, because both classes carry the same
workflow type name. Then we send the signal the override endpoint sends.

Verified by re-introducing the bug: move the patched() read back to the top of _hold_signoff_gate and
test_the_override_releases_a_study_that_was_already_stranded fails -- the study re-verifies and ends
back in AWAITING_SIGNOFF.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.client import WorkflowFailureError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402  -- #57

# The frozen pre-#57 workflow must be importable BY NAME, not just loadable by path: Temporal's
# workflow sandbox re-imports a workflow's module inside the sandbox, and a module conjured with
# importlib.spec_from_file_location does not exist there. orchestrator/tests is not a package, so
# put its fixtures dir on the path rather than make the whole test tree one.
sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from pre57_workflow import StudyWorkflow as Pre57StudyWorkflow  # noqa: E402

CTX = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_stranded",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {},
    "meta": {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": "b7ad6b7169203331",
        "emittedAt": "2026-06-26T00:00:00Z",
        "source": "test",
    },
}


class _Spy:
    def __init__(self):
        self.verifies = 0
        self.dispatches = 0
        self.dead_letters: list = []


def _activities(spy: _Spy):
    @activity.defn(name="call_agent_skill_activity")
    async def call_agent_skill_activity(agent, skill_id, payload):
        if skill_id == "report.verify":
            spy.verifies += 1
            # Nobody edited the report, so verification can only ever re-derive the same FAIL. That
            # is the premise of #56: re-verifying is not a way out of this gate, it IS the trap.
            return {"verificationStatus": "FAIL", "requiresHumanReview": True, "issues": []}
        if skill_id == "triage.score":
            return {"priorityTier": "ROUTINE", "priorityScore": 50}
        if skill_id == "comms.dispatch":
            spy.dispatches += 1
            return {"dispatchStatus": "SENT", "channelResults": []}
        return {"ok": True}

    @activity.defn(name="publish_priority_activity")
    async def publish_priority_activity(workflow_id, study_instance_uid, triage):
        return None

    @activity.defn(name="escalate_activity")
    async def escalate_activity(workflow_id, reason, escalation=None):
        return None

    @activity.defn(name="load_escalation_policy_activity")
    async def load_escalation_policy_activity(tier):
        return [{"level": 1, "afterMinutes": 5, "targetRole": "reading-radiologist",
                 "channels": ["in-app"], "urgency": "routine"}]

    @activity.defn(name="record_signoff_abandoned_activity")
    async def record_signoff_abandoned_activity(workflow_id, tier, pages, *a, **k):
        spy.dead_letters.append(workflow_id)
        return None

    @activity.defn(name="record_policy_failure_activity")
    async def record_policy_failure_activity(workflow_id, tier, *a, **k):
        return None

    return [
        call_agent_skill_activity,
        publish_priority_activity,
        escalate_activity,
        load_escalation_policy_activity,
        record_signoff_abandoned_activity,
        record_policy_failure_activity,
    ]


async def _park_a_study_on_pre57_code(client, spy):
    """Worker A: the code running in production today. Returns a handle to a study left OPEN and
    parked at the sign-off gate, with no patch marker anywhere in its history."""
    async with Worker(client, task_queue=TASK_QUEUE, workflows=[Pre57StudyWorkflow],
                      activities=_activities(spy), max_cached_workflows=0):
        handle = await client.start_workflow(
            Pre57StudyWorkflow.run, CTX, id="wf-stranded", task_queue=TASK_QUEUE)
        await handle.signal(Pre57StudyWorkflow.report_finalized,
                            {"diagnosticReportId": "DiagnosticReport/1"})
        for _ in range(600):
            if await handle.query(Pre57StudyWorkflow.current_state) == "AWAITING_SIGNOFF":
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("the pre-#57 fixture never reached the sign-off gate")
    return handle


async def _drive() -> tuple[dict | None, _Spy, str]:
    spy = _Spy()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle = await _park_a_study_on_pre57_code(env.client, spy)
        verifies_when_stranded = spy.verifies

        # --- the deploy: #57 picks up the very same execution -------------------------
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                          activities=_activities(spy), max_cached_workflows=0):
            # Exactly what POST /signoff/{workflowId}/override sends.
            await handle.signal(StudyWorkflow.signoff_acknowledged, {
                "acknowledgedBy": "Practitioner/dr-rao",
                "reason": "reviewed with the referrer; the critical was already phoned through",
                "acknowledgedAt": "2026-07-13T10:00:00Z",
            })
            try:
                result = await asyncio.wait_for(handle.result(), timeout=30)
            except (asyncio.TimeoutError, WorkflowFailureError):
                state = await handle.query(StudyWorkflow.current_state)
                return None, spy, state
        spy.verifies -= verifies_when_stranded   # count only what happened AFTER the override
        return result, spy, result["finalState"]


@pytest.fixture(scope="module")
def deployed():
    """One deploy, shared by both assertions -- it stands up a Temporal environment and two workers."""
    return asyncio.run(_drive())


def test_the_override_releases_a_study_that_was_already_stranded(deployed):
    """The point of the whole MR. A study parked at the gate before #57 shipped must archive when a
    radiologist overrides -- and must NOT be sent back through report.verify to re-derive the same
    FAIL that put it there."""
    result, spy, state = deployed

    assert result is not None, (
        f"the stranded study never archived (stuck in {state}) -- the override is inert for exactly "
        f"the studies #56 filed"
    )
    assert state == "ARCHIVED"
    assert spy.verifies == 0, "the unchanged report was re-verified after the ack; that is the loop"
    assert spy.dispatches == 1, "the study must reach COMMUNICATE -- its critical finding is undispatched"
    assert spy.dead_letters == [], "an acknowledged gate must not be dead-lettered as abandoned"


def test_the_acknowledgement_is_on_the_record_of_a_rescued_study(deployed):
    """Who released a held safety verdict, and why, has to survive onto the workflow record -- for a
    study inherited from old code just as much as for a fresh one."""
    result, _, _ = deployed

    assert result is not None
    signoff = result["signoff"]
    assert signoff["status"] == "ACKNOWLEDGED"
    assert signoff["acknowledgedBy"] == "Practitioner/dr-rao"
    assert "already phoned through" in signoff["reason"]
    assert signoff["acknowledgedAt"]

    # And the FAIL it was gated on rides along to COMMUNICATE, per #57's second requirement.
    assert result["verification"]["verificationStatus"] == "FAIL"
