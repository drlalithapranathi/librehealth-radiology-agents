"""#26: a study already in flight must survive the deploy that adds the pre-sign block.

A Temporal workflow replays its own history against the CURRENT code. `_presign_impression` inserts
activity commands into the middle of a path that in-flight studies have already walked, so without
a patch marker a study parked at the sign-off gate wakes after the redeploy, replays, finds commands
that were not there when it ran, and dies with NondeterminismError -- wedged, mid-gate, on a report
awaiting a radiologist.

The fixture is a REAL history, recorded by running the workflow as it existed at fac9d69 (main
immediately before #26). It contains no patch marker, because that code had none. Replaying it is
the same determinism check the worker performs on every replay, so a failure here is not a test
failure -- it is the production failure.

Re-record (only if the pre-#26 shape ever legitimately changes):
    git worktree add --detach /tmp/wt fac9d69 && cd /tmp/wt && python <recorder>
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

_PRE26_HISTORY = Path(__file__).parent / "fixtures" / "presign_pre26_history.json"


def _decoded_payloads(history: dict) -> str:
    """Every payload in the history, base64-decoded and concatenated -- activity inputs and results
    are stored encoded, so a plain substring search over the raw JSON would find nothing."""
    out = []

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


def test_the_fixture_really_predates_the_patch():
    """Guard the guard. Two ways this test could pass while proving nothing:

    1. the fixture already carries the patch marker (then patched() trivially returns True), or
    2. the fixture's study has no COMPLETE aiFinding -- in which case the COMPLETE gate (#26)
       short-circuits _presign_impression before it schedules anything, no commands diverge, and
       the replay succeeds with or without the marker.

    So the fixture must be pre-patch AND be a study the pre-sign block WOULD act on. Verified by
    re-introducing the bug: with `patched()` removed, the replay below fails.
    """
    raw = _PRE26_HISTORY.read_text()
    assert "presign-impression-v1" not in raw          # (1) genuinely pre-patch
    assert "write_presign_impression_activity" not in raw
    # (2) the block would fire on this study. Activity payloads are base64 in the history JSON, so
    # decode them to find the aiFindings the workflow actually saw.
    assert "COMPLETE" in _decoded_payloads(json.loads(raw))


def test_an_in_flight_study_replays_cleanly_across_the_presign_deploy():
    """The whole point of workflow.patched(): an OLD history skips the new block (it never happened
    for that study), while every NEW study takes it.

    Without the marker the replay diverges the moment it reaches the inserted block and the
    workflow dies -- i.e. a study parked at the sign-off gate can never be woken again.
    """
    history = WorkflowHistory.from_json(
        "wf-pre26", json.loads(_PRE26_HISTORY.read_text()))

    asyncio.run(
        Replayer(workflows=[StudyWorkflow]).replay_workflow(history)
    )
