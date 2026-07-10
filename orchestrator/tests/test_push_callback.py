"""A2A push-notification callback path (#24): ingress receiver -> skill_completed signal ->
_call_push wait. Bodies mirror the REAL callback shapes observed from the live 1.0.3 sender
(initial task snapshot, WORKING status, artifact with the result, terminal status — as
separate POSTs). Skipped when the orchestrator's deps aren't installed.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio", reason="temporalio not installed")
from temporalio import activity, workflow  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from fastapi import HTTPException  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402


def _artifact_body(task_id: str, data: dict) -> dict:
    return {"artifactUpdate": {"taskId": task_id, "contextId": "ctx",
                               "artifact": {"artifactId": "a1", "parts": [{"data": data}]}}}


def _status_body(task_id: str, state: str) -> dict:
    return {"statusUpdate": {"taskId": task_id, "contextId": "ctx", "status": {"state": state}}}


RESULT = {"priorityTier": "URGENT", "priorityScore": 88.0, "workflowId": "wf_p24"}


class _FakeHandle:
    def __init__(self, sink, wf_id):
        self._sink, self._wf = sink, wf_id

    async def signal(self, signal, arg):
        self._sink.append((self._wf, signal, arg))


class _FakeClient:
    def __init__(self):
        self.signals: list = []

    def get_workflow_handle(self, wf_id):
        return _FakeHandle(self.signals, wf_id)


@pytest.fixture(autouse=True)
def _fresh_ingress(monkeypatch):
    client = _FakeClient()
    ingress._client = client
    ingress._PUSH_PARTS.clear()
    monkeypatch.setattr(ingress, "A2A_CALLBACK_TOKEN", "")
    yield client
    ingress._client = None
    ingress._PUSH_PARTS.clear()


# ---- ingress receiver -------------------------------------------------------------

def test_artifact_then_terminal_relays_the_result(_fresh_ingress):
    async def scenario():
        r1 = await ingress.a2a_push_callback("wf_p24", _artifact_body("t1", RESULT),
                                             x_a2a_notification_token="")
        r2 = await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_COMPLETED"),
                                             x_a2a_notification_token="")
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1 == {"buffered": "t1"}
    assert r2 == {"relayed": "t1"}
    # exactly one signal, to the workflow named in the URL, carrying the buffered result
    assert _fresh_ingress.signals == [
        ("wf_p24", StudyWorkflow.skill_completed, {"taskId": "t1", "result": RESULT})]
    assert ingress._PUSH_PARTS == {}  # buffer reclaimed


def test_non_terminal_events_are_ignored(_fresh_ingress):
    async def scenario():
        return await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_WORKING"),
                                               x_a2a_notification_token="")

    assert asyncio.run(scenario()) == {"ignored": "non-terminal event"}
    assert _fresh_ingress.signals == []


def test_failed_terminal_relays_failure(_fresh_ingress):
    async def scenario():
        await ingress.a2a_push_callback("wf_p24", _artifact_body("t1", RESULT),
                                        x_a2a_notification_token="")
        return await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_FAILED"),
                                               x_a2a_notification_token="")

    asyncio.run(scenario())
    assert _fresh_ingress.signals == [
        ("wf_p24", StudyWorkflow.skill_completed, {"taskId": "t1", "failed": True})]
    assert ingress._PUSH_PARTS == {}  # failure path also reclaims the buffer


def test_completed_without_artifact_is_a_failure(_fresh_ingress):
    """A COMPLETED task whose artifact half was lost (the sender never retries a POST) has no
    result to relay: report failure so the workflow re-runs the skill instead of hanging."""
    async def scenario():
        return await ingress.a2a_push_callback("wf_p24",
                                               _status_body("t9", "TASK_STATE_COMPLETED"),
                                               x_a2a_notification_token="")

    asyncio.run(scenario())
    assert _fresh_ingress.signals == [
        ("wf_p24", StudyWorkflow.skill_completed, {"taskId": "t9", "failed": True})]


# A contract-valid triage.score output (integer fields arrive as floats via protobuf Struct;
# Draft 2020-12 accepts integral floats as `integer`).
VALID_TRIAGE = {
    "schemaVersion": "1.0.0", "workflowId": "wf_p24", "priorityScore": 88,
    "priorityTier": "URGENT", "agentVersion": "0.1.0", "computedAt": "2026-07-09T00:00:00Z",
}


def test_valid_result_passes_ingress_revalidation(_fresh_ingress):
    async def scenario():
        await ingress.a2a_push_callback("wf_p24", _artifact_body("t1", VALID_TRIAGE),
                                        skill="triage.score", x_a2a_notification_token="")
        await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_COMPLETED"),
                                        skill="triage.score", x_a2a_notification_token="")

    asyncio.run(scenario())
    (wf, sig, event), = _fresh_ingress.signals
    assert event["taskId"] == "t1" and "failed" not in event
    assert event["result"]["priorityTier"] == "URGENT"


def test_nonconforming_result_is_relayed_as_failure(_fresh_ingress):
    """The endpoint may be reachable without the token, so a delivered result that violates the
    skill's output contract (forged or agent bug) must not enter workflow state as a result."""
    async def scenario():
        await ingress.a2a_push_callback("wf_p24", _artifact_body("t1", RESULT),  # missing fields
                                        skill="triage.score", x_a2a_notification_token="")
        await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_COMPLETED"),
                                        skill="triage.score", x_a2a_notification_token="")

    asyncio.run(scenario())
    assert _fresh_ingress.signals == [
        ("wf_p24", StudyWorkflow.skill_completed, {"taskId": "t1", "failed": True})]


def test_push_parts_buffer_is_size_capped(_fresh_ingress, monkeypatch):
    """Artifact-only POSTs with unique taskIds (orphans — or a memory-DoS on the tokenless dev
    posture) must not grow the buffer without bound: oldest entries are evicted."""
    monkeypatch.setattr(ingress, "_PUSH_PARTS_CAP", 5)

    async def scenario():
        for i in range(9):
            await ingress.a2a_push_callback("wf_p24", _artifact_body(f"t{i}", RESULT),
                                            x_a2a_notification_token="")

    asyncio.run(scenario())
    assert len(ingress._PUSH_PARTS) == 5
    assert set(ingress._PUSH_PARTS) == {"t4", "t5", "t6", "t7", "t8"}  # oldest evicted first


def test_bad_token_is_rejected(_fresh_ingress, monkeypatch):
    monkeypatch.setattr(ingress, "A2A_CALLBACK_TOKEN", "expected")

    async def scenario():
        await ingress.a2a_push_callback("wf_p24", _status_body("t1", "TASK_STATE_COMPLETED"),
                                        x_a2a_notification_token="wrong")

    with pytest.raises(HTTPException) as err:
        asyncio.run(scenario())
    assert err.value.status_code == 401
    assert _fresh_ingress.signals == []


def test_unparseable_body_is_422_not_500(_fresh_ingress):
    async def scenario():
        # Structurally impossible StreamResponse (proto ParseError); softer garbage like a
        # wrong-typed `task` string parses to an empty event and is simply ignored.
        await ingress.a2a_push_callback("wf_p24", {"statusUpdate": 5},
                                        x_a2a_notification_token="")

    with pytest.raises(HTTPException) as err:
        asyncio.run(scenario())
    assert err.value.status_code == 422


def test_soft_garbage_degrades_to_ignored(_fresh_ingress):
    async def scenario():
        return await ingress.a2a_push_callback("wf_p24", {"task": "not-an-object"},
                                               x_a2a_notification_token="")

    assert asyncio.run(scenario()) == {"ignored": "non-terminal event"}
    assert _fresh_ingress.signals == []


# ---- workflow signal handler (pure unit) ------------------------------------------

def test_skill_completed_signal_is_first_write_wins():
    """A duplicated terminal can deliver both signals in ONE workflow activation (before the
    waiter wakes): the second — a synthesized failure from an empty re-popped buffer — must not
    overwrite the good result."""
    wf = StudyWorkflow()
    wf.skill_completed({"taskId": "t1", "result": {"a": 1}})
    wf.skill_completed({"taskId": "t1", "failed": True})  # late duplicate: ignored
    assert wf._skill_results == {"t1": {"a": 1}}
    wf.skill_completed({"taskId": "t2", "failed": True})
    assert wf._skill_results["t2"] == {"__failed__": True}
    wf.skill_completed({})  # no taskId -> ignored
    assert set(wf._skill_results) == {"t1", "t2"}


def test_skill_completed_buffer_is_size_capped():
    """Orphaned taskIds (an activity retry's duplicate task) are never awaited or popped, so the
    handler evicts oldest-first at PUSH_RESULT_CAP instead of growing workflow state forever."""
    from orchestrator.workflow import PUSH_RESULT_CAP
    wf = StudyWorkflow()
    for i in range(PUSH_RESULT_CAP + 3):
        wf.skill_completed({"taskId": f"t{i}", "result": {"i": i}})
    assert len(wf._skill_results) == PUSH_RESULT_CAP
    assert "t0" not in wf._skill_results and f"t{PUSH_RESULT_CAP + 2}" in wf._skill_results


# ---- _call_push on the real Temporal test server -----------------------------------

@activity.defn(name="start_agent_skill_activity")
async def mock_start_agent(agent: str, skill_id: str, payload: dict, workflow_id: str) -> str:
    return "task-99"


@workflow.defn(sandboxed=False)  # the sandbox forbids subclassing a proxied workflow class
class _PushProbeWorkflow(StudyWorkflow):
    """Test-only workflow: runs exactly one push-mode skill call and returns its result."""

    @workflow.run
    async def run(self, args: list) -> dict:  # type: ignore[override]
        agent, skill_id, payload = args
        return await self._call_push(agent, skill_id, payload)


def test_call_push_waits_for_the_callback_signal():
    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[_PushProbeWorkflow],
                              activities=[mock_start_agent]):
                handle = await env.client.start_workflow(
                    _PushProbeWorkflow.run, ["worklist-triage", "triage.score", {"p": 1}],
                    id="wf_push_probe", task_queue=TASK_QUEUE,
                )
                # The workflow is parked on wait_condition until the callback relay signals it.
                await asyncio.sleep(0.2)
                await handle.signal(StudyWorkflow.skill_completed,
                                    {"taskId": "task-99", "result": RESULT})
                return await handle.result()

    assert asyncio.run(scenario()) == RESULT


def _minting_start_agent():
    """Mock ACT_START_AGENT that mints a fresh taskId per call, like the real agent does."""
    count = {"n": 0}

    @activity.defn(name="start_agent_skill_activity")
    async def start(agent: str, skill_id: str, payload: dict, workflow_id: str) -> str:
        count["n"] += 1
        return f"task-{count['n']}"

    return start


def test_call_push_rethrows_a_failed_attempt_then_succeeds():
    """A failed callback re-runs the skill (fresh agent task); the retry's result is returned.
    Signals are sent up-front — the handler stores early arrivals, so no timing race."""
    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[_PushProbeWorkflow],
                              activities=[_minting_start_agent()]):
                handle = await env.client.start_workflow(
                    _PushProbeWorkflow.run, ["worklist-triage", "triage.score", {"p": 1}],
                    id="wf_push_retry", task_queue=TASK_QUEUE,
                )
                await handle.signal(StudyWorkflow.skill_completed,
                                    {"taskId": "task-1", "failed": True})
                await handle.signal(StudyWorkflow.skill_completed,
                                    {"taskId": "task-2", "result": RESULT})
                return await handle.result()

    assert asyncio.run(scenario()) == RESULT


def test_call_push_exhausts_attempts_and_fails_the_workflow_cleanly():
    """No callback ever arrives (every POST lost): each attempt's wait times out, and after
    PUSH_SKILL_ATTEMPTS the workflow FAILS with ApplicationError — the regression here was a
    plain RuntimeError, which is not a Temporal failure exception and left the workflow RUNNING
    in a hot workflow-task retry loop forever (wedged, never escalating, never archiving)."""
    from temporalio.client import WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    async def scenario():
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[_PushProbeWorkflow],
                              activities=[_minting_start_agent()]):
                handle = await env.client.start_workflow(
                    _PushProbeWorkflow.run, ["worklist-triage", "triage.score", {"p": 1}],
                    id="wf_push_exhaust", task_queue=TASK_QUEUE,
                )
                return await handle.result()

    with pytest.raises(WorkflowFailureError) as err:
        asyncio.run(scenario())
    assert isinstance(err.value.cause, ApplicationError)
    assert err.value.cause.type == "PushSkillError"
