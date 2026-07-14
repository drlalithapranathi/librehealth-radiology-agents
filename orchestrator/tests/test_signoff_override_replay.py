"""#57: a study that already went through the OLD sign-off gate must still replay.

A Temporal workflow replays its own history against the CURRENT code. #57 changes the gate path in
two ways that both diverge from what pre-#57 studies recorded:

  1. the gate used to end in an unbounded `wait_condition` for the ack; it now ends bounded, with a
     dead-letter activity on the abandoned path; and
  2. an ack used to send the study back through report.verify (the re-verify loop); it now breaks
     straight out to COMMUNICATE.

So a pre-#57 history has a SECOND report.verify activity recorded where this code schedules
comms.dispatch. Replaying it without the patch marker is a command/event mismatch -- and the
histories this hits are the ones that went through the sign-off gate, i.e. the studies a reviewer is
most likely to query.

The fixture is a REAL history, recorded by running the workflow as it existed at the parent commit
(main, immediately before #57): FAIL -> gate -> ack -> RE-VERIFY -> PASS -> archived. It carries no
marker, because that code had none.

Re-record (only if the pre-#57 shape ever legitimately changes):
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

_PRE57_HISTORY = Path(__file__).parent / "fixtures" / "signoff_pre57_history.json"


def _decoded_payloads(history: dict) -> str:
    """Every payload in the history, base64-decoded -- activity inputs and results are stored
    encoded, so a plain substring search over the raw JSON would find nothing."""
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


def test_the_fixture_really_predates_the_override():
    """Guard the guard. Two ways this test could pass while proving nothing:

    1. the fixture already carries the patch marker (then patched() trivially returns True), or
    2. the fixture never actually entered the sign-off gate -- a study that PASSed verification
       never touches the changed path at all, so it replays cleanly with or without the marker.

    So the fixture must be pre-patch AND be a study that went through the gate: it must carry a
    verification FAIL and the pre-#57 re-verify. Verified by re-introducing the bug: with
    `patched()` removed, the replay below fails with NondeterminismError.
    """
    raw = _PRE57_HISTORY.read_text()
    assert "signoff-override-v1" not in raw                     # (1) genuinely pre-patch
    assert "record_signoff_abandoned_activity" not in raw

    # (2) this study really went through the gate: it FAILed verification, needed a human, and was
    # then re-verified -- the loop #57 removes. Payloads are base64 in the history JSON, and
    # Temporal's JSON converter writes them COMPACT (no space after the colon).
    payloads = _decoded_payloads(json.loads(raw))
    assert '"verificationStatus":"FAIL"' in payloads, "the fixture never entered the sign-off gate"
    assert '"requiresHumanReview":true' in payloads
    assert payloads.count("report.verify") >= 2, (
        "the fixture shows only one verify: the pre-#57 re-verify loop is not in this history, so "
        "the replay would not exercise the path #57 changes"
    )


def test_a_study_that_went_through_the_old_gate_still_replays():
    """The whole point of workflow.patched(): an OLD history keeps the OLD shape (unbounded hold,
    re-verify after the ack), while every NEW study takes the bounded, terminal gate.

    Without the marker this raises NondeterminismError -- and it raises for exactly the studies that
    hit the sign-off gate, which are the ones anyone would go back and query.
    """
    history = WorkflowHistory.from_json(
        "wf-pre57", json.loads(_PRE57_HISTORY.read_text()))

    asyncio.run(Replayer(workflows=[StudyWorkflow]).replay_workflow(history))
