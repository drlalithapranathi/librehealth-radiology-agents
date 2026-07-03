"""#17 acceptance: an orchestrator dispatch round-trips against the LIVE Communications agent.

Serves this agent's real handler over a loopback A2A server (uvicorn) and calls comms.dispatch
through the real a2a-sdk client (radagent_common.client) — the same path the orchestrator's
activity uses. Skipped unless the a2a extra is installed, so the no-a2a agent-tests lane (which
proves golden-rule 4) stays green.
"""
from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")
import uvicorn  # noqa: E402

from radagent_common.a2a import build_agent_app  # noqa: E402
from radagent_common.client import call_agent_skill  # noqa: E402
from handler import handle  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def comms_agent_url():
    """Run the Communications agent on a loopback port for the duration of a test."""
    port = _free_port()
    app = build_agent_app("communications", handle).build()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

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
        raise RuntimeError("communications agent did not become ready in time")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


async def test_orchestrator_dispatch_round_trips(comms_agent_url):
    out = await call_agent_skill(
        comms_agent_url,
        "comms.dispatch",
        {"studyContext": {"workflowId": "wf_it_17"},
         "impression": {"criticalFlags": [{"label": "aortic dissection", "severity": "critical"}]}},
    )
    assert out["workflowId"] == "wf_it_17"
    assert out["dispatchStatus"] == "SENT"
    # criticality routed to the on-call pager, round-tripped over real A2A transport
    assert "oncall-pager" in [c["channel"] for c in out["channelResults"]]
    assert out["agentVersion"] == "0.1.0"
