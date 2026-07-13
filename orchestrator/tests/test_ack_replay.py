"""#52: adding the ack loop must not break the studies we have ALREADY archived.

A Temporal workflow replays its own history against the CURRENT code. The ack loop appends its
commands at the very end of the path, so an OPEN study replays fine without a patch marker -- but a
CLOSED one does not, and closed histories are replayed: a QUERY against a finished study replays its
whole history (so does a reset). In a pre-#52 history the event after comms.dispatch is
WorkflowExecutionCompleted, while this code now schedules comms.checkAck there. That is a
command/event mismatch on every study already in the archive: query it after the deploy and it
raises NondeterminismError instead of answering.

The fixture is a REAL history, recorded by running the workflow as it existed at the parent commit
(52-critcom-agent, immediately before the ack loop). It carries no marker, because that code had
none. Replaying it is the same determinism check the worker performs, so a failure here is not a
test failure -- it is the production failure.

Re-record (only if the pre-#52 shape ever legitimately changes):
    git worktree add --detach /tmp/wt <parent> && cd /tmp/wt && python <recorder>
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio.client import WorkflowHistory  # noqa: E402
from temporalio.worker import Replayer  # noqa: E402

from orchestrator.workflow import StudyWorkflow  # noqa: E402

_PRE_ACK_HISTORY = Path(__file__).parent / "fixtures" / "ack_preloop_history.json"


def _decoded_payloads(history: dict) -> str:
    """Every payload in the history, base64-decoded and concatenated -- activity inputs and results
    are stored encoded, so a plain substring search over the raw JSON would find nothing."""
    out: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            data = node.get("data")
            if isinstance(data, str) and node.keys() & {"metadata", "data"}:
                try:
                    out.append(base64.b64decode(data).decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001 - non-utf8 payloads are not what we're looking for
                    pass
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(history)
    return "".join(out)


def test_the_fixture_really_predates_the_ack_loop():
    """Guard the guard. Two ways this test could pass while proving nothing:

    1. the fixture already carries the patch marker (then patched() trivially returns True), or
    2. the fixture's dispatch opened no ack clock -- no taskId, no deadline -- in which case
       _await_ack returns immediately, schedules nothing, and the replay succeeds with or without
       the marker.

    So the fixture must be pre-patch AND be a study the ack loop WOULD act on: a CRITICAL dispatch
    carrying a taskId and a deadline. Verified by re-introducing the bug: with `patched()` removed,
    the replay below fails with NondeterminismError.
    """
    raw = _PRE_ACK_HISTORY.read_text()
    assert "critcom-ack-loop-v1" not in raw          # (1) genuinely pre-patch
    assert "comms.checkAck" not in raw

    # (2) the loop would fire on this study. Activity payloads are base64 in the history JSON, so
    # decode them to find the dispatch result the workflow actually saw.
    payloads = _decoded_payloads(json.loads(raw))
    assert '"taskId"' in payloads, "the fixture's dispatch opened no ack clock: nothing to watch"
    assert '"deadline"' in payloads

    # ...and it really is a CLOSED study -- the case a patch marker is needed for.
    assert json.loads(raw)["events"][-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


def test_an_archived_study_still_replays_after_the_ack_loop_ships():
    """The whole point of workflow.patched(): an OLD history skips the new block (it never happened
    for that study), while every NEW study takes it.

    Without the marker, replaying an archived study diverges the moment it reaches the inserted
    block -- i.e. querying any study we archived before this deploy raises instead of answering.
    """
    history = WorkflowHistory.from_json(
        "wf-pre-ack", json.loads(_PRE_ACK_HISTORY.read_text()))

    asyncio.run(Replayer(workflows=[StudyWorkflow]).replay_workflow(history))
