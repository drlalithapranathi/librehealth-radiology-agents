"""Integration test for radagent_common.client.call_agent_skill (issue #7).

Serves the project's mock A2A agent (mocks/mock_agent.py) on a real loopback port via uvicorn
and calls it through the real a2a-sdk client, asserting the skill JSON round-trips.

Needs the `a2a` extra (a2a-sdk[http-server]) + uvicorn; skipped when a2a-sdk isn't installed so
the core (no-a2a) test lane — which proves golden-rule 4 (handlers never import a2a.*) — stays green.
"""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest

pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")
import uvicorn  # noqa: E402

from radagent_common.a2a import build_agent_app  # noqa: E402
from radagent_common.client import call_agent_skill  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_mock_handle():
    """Import mocks/mock_agent.py by path (it lives outside any package)."""
    spec = importlib.util.spec_from_file_location("mock_agent", REPO_ROOT / "mocks" / "mock_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.handle


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def mock_agent_url():
    """Run the mock worklist-triage agent on a loopback port for the duration of a test."""
    port = _free_port()
    app = build_agent_app("worklist-triage", _load_mock_handle()).build()
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
        raise RuntimeError("mock agent did not become ready in time")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


async def test_call_agent_skill_round_trip(mock_agent_url):
    out = await call_agent_skill(
        mock_agent_url, "triage.score", {"studyContext": {"workflowId": "wf_it_7"}}
    )
    assert out["workflowId"] == "wf_it_7"
    assert out["priorityTier"] == "ROUTINE"
    assert out["agentVersion"] == "mock"
