"""Integration + unit tests for the A2A client/server transport (issues #7 and #8).

Serves the project's mock A2A agent (mocks/mock_agent.py) on a real loopback port via uvicorn
and calls it through the real a2a-sdk client, asserting the skill round-trips over a typed
DataPart (#8) and that the card is served at the pinned well-known path (#8).

Needs the `a2a` extra (a2a-sdk[http-server]) + uvicorn; skipped when a2a-sdk isn't installed so
the core (no-a2a) test lane — which proves golden-rule 4 (handlers never import a2a.*) — stays green.
"""
from __future__ import annotations

import importlib.util
import socket
import threading
import time
import types
from pathlib import Path

import httpx
import pytest

pytest.importorskip("a2a", reason="a2a extra (a2a-sdk) not installed")
import uvicorn  # noqa: E402
from a2a.helpers import new_data_message, new_text_message, new_data_part, get_data_parts, get_text_parts  # noqa: E402
from a2a.types import StreamResponse, Task, Artifact  # noqa: E402

from radagent_common.a2a import build_agent_app, WELL_KNOWN_CARD_PATH, _extract_payload  # noqa: E402
from radagent_common.client import call_agent_skill, skill_message, envelope, _response_data_parts  # noqa: E402
from radagent_common.validation import ContractError  # noqa: E402

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
            if httpx.get(f"{base}{WELL_KNOWN_CARD_PATH}", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("mock agent did not become ready in time")

    yield base

    server.should_exit = True
    thread.join(timeout=5)


async def test_call_agent_skill_round_trip(mock_agent_url):
    """#8 acceptance: orchestrator -> agent -> orchestrator round-trip over a DataPart."""
    out = await call_agent_skill(
        mock_agent_url, "triage.score", {"studyContext": {"workflowId": "wf_it_7"}}
    )
    assert out["workflowId"] == "wf_it_7"
    assert out["priorityTier"] == "ROUTINE"
    assert out["agentVersion"] == "mock"
    # DataPart fingerprint: protobuf Struct returns the int 50 as float 50.0. A text/JSON transport
    # would keep it int, so this also proves the payload really rode a DataPart end-to-end.
    assert isinstance(out["priorityScore"], float) and out["priorityScore"] == 50


def test_payload_travels_as_data_part():
    """The client's ACTUAL message (skill_message) is a typed DataPart, not a text part."""
    parts = skill_message("triage.score", {"studyContext": {"workflowId": "wf_x"}}).parts
    assert get_text_parts(parts) == []
    assert get_data_parts(parts) == [
        {"skillId": "triage.score", "payload": {"studyContext": {"workflowId": "wf_x"}}}
    ]


def test_server_rejects_non_datapart_message():
    """_extract_payload accepts only a dict DataPart; a text part or non-dict data -> ContractError."""
    text_ctx = types.SimpleNamespace(message=new_text_message("not a data part"))
    with pytest.raises(ContractError):
        _extract_payload(text_ctx)
    nondict_ctx = types.SimpleNamespace(message=new_data_message([1, 2, 3]))
    with pytest.raises(ContractError):
        _extract_payload(nondict_ctx)


def test_datapart_preserves_structure_except_numbers():
    """Structure survives the DataPart (bool/str/null/array/nested); numbers coerce to float."""
    payload = {"flag": True, "tags": ["a", "b"], "nested": {"count": 312}, "note": "x", "nil": None}
    got = get_data_parts(new_data_message(envelope("s", payload)).parts)[0]["payload"]
    assert got["flag"] is True
    assert got["note"] == "x"
    assert got["nil"] is None
    assert got["tags"] == ["a", "b"]
    assert got["nested"]["count"] == 312 and isinstance(got["nested"]["count"], float)


def test_response_data_parts_reads_task_reply():
    """Regression: a Task-wrapped reply (data in an artifact) is read, not silently dropped."""
    resp = StreamResponse(task=Task(artifacts=[Artifact(parts=[new_data_part({"ok": True})])]))
    assert _response_data_parts(resp) == [{"ok": True}]


def test_card_served_at_pinned_well_known_path(mock_agent_url):
    """#8: the well-known path is pinned to the SDK's canonical value and actually serves the card."""
    assert WELL_KNOWN_CARD_PATH == "/.well-known/agent-card.json"
    r = httpx.get(f"{mock_agent_url}{WELL_KNOWN_CARD_PATH}", timeout=5)
    assert r.status_code == 200
    assert r.json()["name"] == "Worklist Triage Agent"
