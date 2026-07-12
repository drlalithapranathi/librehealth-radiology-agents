"""#54: a study already parked at the sign-off gate must survive the deploy that adds the
escalation-policy dead-letter write.

A Temporal workflow replays its own history against the CURRENT code. `_record_policy_failure`
inserts an activity command into the sign-off-gate fallback branch, a path that a study parked at
the gate has already walked. Without a patch marker, that study wakes after the redeploy, replays,
finds a command that was not there when it ran, and dies with NondeterminismError -- wedged mid-gate.
And because the fallback fires precisely when the escalation policy is broken, deploying this fix
without the guard could wedge many parked studies at once.

The fixture is a REAL history, recorded by running the workflow as it existed at origin/main
immediately before #54, driven through the policy-load-failure fallback. It carries no patch marker,
because that code had none. Replaying it is the same determinism check the worker performs on every
replay, so a failure here is not a test failure -- it is the production failure.

Re-record (only if the pre-#54 fallback shape ever legitimately changes): run the workflow at a
pre-#54 commit through the fallback path (load_escalation_policy fails) and dump handle history.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio.client import WorkflowHistory  # noqa: E402
from temporalio.worker import Replayer  # noqa: E402

from orchestrator.workflow import StudyWorkflow  # noqa: E402

_PRE54_HISTORY = Path(__file__).parent / "fixtures" / "deadletter_pre54_history.json"


def _scheduled_activities(history: dict) -> set[str]:
    return {
        e["activityTaskScheduledEventAttributes"]["activityType"]["name"]
        for e in history.get("events", [])
        if "activityTaskScheduledEventAttributes" in e
    }


def test_the_fixture_really_predates_the_patch():
    """Guard the guard. The fixture must be pre-patch AND be a study the dead-letter write WOULD
    act on, or the replay proves nothing.

    Pre-patch: it carries neither the marker nor the new activity. Would-act-on: it went through
    the sign-off gate (load_escalation_policy_activity) and into the fallback page (escalate_activity
    scheduled), which is exactly the branch _record_policy_failure is inserted into. Verified by
    re-introducing the bug: with `workflow.patched(...)` removed, the replay below fails with
    NondeterminismError.
    """
    raw = _PRE54_HISTORY.read_text()
    assert "policy-dead-letter-v1" not in raw            # genuinely pre-patch (no marker)
    assert "record_policy_failure_activity" not in raw   # the new command is absent
    scheduled = _scheduled_activities(json.loads(raw))
    assert "load_escalation_policy_activity" in scheduled  # the gate tried to load a ladder
    assert "escalate_activity" in scheduled                # the fallback page fired


def test_a_parked_study_replays_cleanly_across_the_deadletter_deploy():
    """The whole point of workflow.patched(): an OLD history skips the new write (it never happened
    for that study), while every NEW study records it.

    Without the marker the replay diverges the moment it reaches the inserted activity command and
    the workflow dies -- i.e. a study parked at the sign-off gate can never be woken again.
    """
    history = WorkflowHistory.from_json(
        "wf-deadletter-pre54", json.loads(_PRE54_HISTORY.read_text()))

    asyncio.run(
        Replayer(workflows=[StudyWorkflow]).replay_workflow(history)
    )
