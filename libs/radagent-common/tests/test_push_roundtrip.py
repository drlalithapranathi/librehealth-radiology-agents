"""#24 acceptance: a push-notification skill call round-trips against a LIVE agent.

Serves a stub triage handler over a loopback A2A server (uvicorn) plus a loopback callback
receiver, then drives the REAL push path end to end: start_agent_skill attaches the push config
-> the agent runs the Task lifecycle -> the SDK's push sender POSTs task events to the callback
URL with the token header -> parse_push_callback extracts the terminal result. Also proves the
unary path is untouched: the same live agent still answers call_agent_skill exactly as before.

Skipped unless the a2a extra is installed (same lane split as the comms roundtrip test).
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from radagent_common.a2a import build_agent_app  # noqa: E402
from radagent_common.client import (  # noqa: E402
    PUSH_TOKEN_HEADER,
    call_agent_skill,
    parse_push_callback,
    start_agent_skill,
)

TRIAGE_OUTPUT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_push_24", "priorityScore": 88,
    "priorityTier": "URGENT", "agentVersion": "0.1.0", "computedAt": "2026-07-09T00:00:00Z",
}


async def _stub_handle(skill_id: str, payload: dict) -> dict:
    return dict(TRIAGE_OUTPUT)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    return server


@pytest.fixture()
def agent_url():
    """A live agent (stub triage handler behind the real factory) on a loopback port."""
    port = _free_port()
    server = _serve(build_agent_app("worklist-triage", _stub_handle).build(), port)
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/.well-known/agent-card.json", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        server.should_exit = True
        raise RuntimeError("agent did not become ready in time")
    yield base
    server.should_exit = True


@pytest.fixture()
def callback_sink():
    """A loopback receiver capturing every push-notification POST (body + token header)."""
    received: list[tuple[dict, str]] = []

    async def receive(request):
        received.append((await request.json(), request.headers.get(PUSH_TOKEN_HEADER, "")))
        return JSONResponse({"ok": True}, status_code=202)

    port = _free_port()
    server = _serve(Starlette(routes=[Route("/cb", receive, methods=["POST"])]), port)
    time.sleep(0.3)  # tiny startup grace; the poll below is the real wait
    yield f"http://127.0.0.1:{port}/cb", received
    server.should_exit = True


async def test_push_skill_round_trips_to_the_callback(agent_url, callback_sink):
    callback_url, received = callback_sink

    task_id = await start_agent_skill(
        agent_url, "triage.score",
        {"studyContext": {"workflowId": "wf_push_24"}},
        callback_url=callback_url, callback_token="s3cret",
    )
    assert task_id  # the agent ACKed with a real A2A task id

    # The sender POSTs the lifecycle as separate events: the result rides an artifact event,
    # the terminal state a later status event. Accumulate exactly as the ingress receiver does.
    deadline = time.monotonic() + 10
    artifact_parts: list[dict] = []
    terminal = None
    while time.monotonic() < deadline and terminal is None:
        for body, token in list(received):
            assert token == "s3cret"  # every callback carries our token header
            parsed = parse_push_callback(body)
            if parsed is None:
                continue
            if parsed["kind"] == "artifact" and parsed["taskId"] == task_id:
                artifact_parts = parsed["parts"]
            elif parsed["kind"] == "terminal" and parsed["taskId"] == task_id:
                terminal = parsed
        await asyncio.sleep(0.05)
    assert terminal is not None, "no terminal push notification arrived"

    assert terminal["state"] == "TASK_STATE_COMPLETED"
    assert artifact_parts, "the artifact event carrying the result never arrived"
    assert artifact_parts[0]["workflowId"] == "wf_push_24"
    assert artifact_parts[0]["priorityTier"] == "URGENT"
    assert artifact_parts[0]["priorityScore"] == 88.0  # protobuf Struct coerces ints to float


async def test_unary_path_is_unchanged_on_a_push_capable_agent(agent_url):
    """The same live agent (now built WITH push support) still answers plain unary calls
    exactly as before — no push config on the send means no Task, no callback, same reply."""
    out = await call_agent_skill(agent_url, "triage.score",
                                 {"studyContext": {"workflowId": "wf_push_24"}})
    assert out["priorityTier"] == "URGENT"
    assert out["workflowId"] == "wf_push_24"
