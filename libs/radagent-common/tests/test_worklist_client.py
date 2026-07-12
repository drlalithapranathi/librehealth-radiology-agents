"""Tests for radagent_common.worklist_client.publish_priority.

Uses httpx.MockTransport so we intercept every network call and never touch a
real Worklist API. Covers: happy-path shape, best-effort semantics on network
error / timeout / non-2xx, and the "never raises" contract that
publish_priority_activity relies on to keep the workflow running.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from radagent_common.worklist_client import publish_priority


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# Capture the real AsyncClient BEFORE any monkeypatch replaces it — the tests patch
# radagent_common.worklist_client.httpx.AsyncClient, which is the same class object as
# this one (import httpx creates a shared reference). Using the captured reference
# inside a patched-in factory avoids infinite recursion.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _client_returning(status_code: int = 204, body: str = "") -> tuple[httpx.MockTransport, list[dict]]:
    """Return a MockTransport that answers every POST with the given status, and a list
    the transport appends every intercepted request-body into for post-hoc assertions."""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.append({
            "method": request.method,
            "url": str(request.url),
            "body": _json.loads(request.content or b"{}"),
        })
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(handler), seen


def _install(monkeypatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(
        "radagent_common.worklist_client.httpx.AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=transport, **kw),
    )


# --- Happy path --------------------------------------------------------------

def test_publish_priority_posts_correct_url_and_payload(monkeypatch):
    transport, seen = _client_returning(204)
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="STAT", priority_score=95,
    ))
    assert ok is True
    assert len(seen) == 1
    assert seen[0]["method"] == "POST"
    assert seen[0]["url"] == "http://worklist-api:8107/priority"
    assert seen[0]["body"] == {
        "studyInstanceUID": "1.2.3", "workflowId": "wf_1",
        "priorityTier": "STAT", "priorityScore": 95,
    }


def test_publish_priority_trims_trailing_slash_on_base_url(monkeypatch):
    """Consumers may build the URL from an env var that ends in `/`. The helper
    must not produce a double-slashed path (some frameworks reject it)."""
    transport, seen = _client_returning(204)
    _install(monkeypatch, transport)
    _run(publish_priority(
        "http://worklist-api:8107///",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="ROUTINE", priority_score=50,
    ))
    assert seen[0]["url"] == "http://worklist-api:8107/priority"


# --- Best-effort: never raise, return False -----------------------------------

def test_publish_priority_swallows_connection_error(monkeypatch, caplog):
    """The Worklist API being down at publish time must NOT fail the activity."""
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(refuse)
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://worklist-api:8107",
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="STAT", priority_score=95,
        ))
    assert ok is False
    assert any("worklist publish failed" in r.message for r in caplog.records)
    assert any("wf_1" in r.message for r in caplog.records)


def test_publish_priority_swallows_timeout(monkeypatch, caplog):
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    transport = httpx.MockTransport(slow)
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://worklist-api:8107",
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="STAT", priority_score=95,
        ))
    assert ok is False


def test_publish_priority_returns_false_on_422(monkeypatch, caplog):
    """The Worklist API rejects malformed payloads with 422. Even that must not
    raise — a bad tier/score in the triage output is a bug worth logging but not
    a reason to fail the workflow."""
    transport, _ = _client_returning(422, body='{"detail":"invalid tier"}')
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://worklist-api:8107",
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="STAT", priority_score=95,
        ))
    assert ok is False
    # Response body snippet appears in the log for triage — but capped.
    joined = " ".join(r.message for r in caplog.records)
    assert "422" in joined
    assert "invalid tier" in joined


def test_publish_priority_returns_false_on_500(monkeypatch, caplog):
    transport, _ = _client_returning(500, body="Internal Server Error")
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="STAT", priority_score=95,
    ))
    assert ok is False


def test_publish_priority_success_on_200(monkeypatch):
    """The Worklist API's /priority endpoint returns 204 today, but the helper must
    treat any 2xx as success so a future change (204 -> 200 with a body) doesn't
    silently start looking like a failure."""
    transport, _ = _client_returning(200, body='{"ok":true}')
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="ROUTINE", priority_score=50,
    ))
    assert ok is True
