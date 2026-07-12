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
    """The Worklist API being down at publish time must NOT fail the activity.
    Retry budget is exercised by the dedicated retry tests below; this one just
    pins the final swallow behavior."""
    import radagent_common.worklist_client as _wc

    async def _instant(_attempt):  # skip backoff sleeps in this test
        return None
    monkeypatch.setattr(_wc, "_sleep_backoff", _instant)

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
    import radagent_common.worklist_client as _wc

    async def _instant(_attempt):
        return None
    monkeypatch.setattr(_wc, "_sleep_backoff", _instant)

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
    a reason to fail the workflow. 4xx does NOT retry (see dedicated test)."""
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
    import radagent_common.worklist_client as _wc

    async def _instant(_attempt):
        return None
    monkeypatch.setattr(_wc, "_sleep_backoff", _instant)

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


# --- Bounded retry behavior (added per #20 review feedback) -----------------
# The helper must self-heal transient blips (network hiccup, Worklist API
# restart) without ever raising or stalling the read path. These tests pin:
#   * the retry count matches _PUBLISH_MAX_ATTEMPTS
#   * 5xx retries but 4xx does not (same bad payload will 422 again)
#   * a transient blip that recovers mid-loop actually succeeds
# Backoff is monkey-patched to zero so tests stay fast.

import radagent_common.worklist_client as _wc


def _zero_backoff(monkeypatch):
    """Patch backoff to zero — retries fire instantly. Preserves the retry
    COUNT semantics we care about testing without slowing the suite."""
    async def _instant(_attempt):
        return None
    monkeypatch.setattr(_wc, "_sleep_backoff", _instant)


def test_publish_priority_retries_on_network_error_then_gives_up(monkeypatch, caplog):
    """A persistent network outage should be tried _PUBLISH_MAX_ATTEMPTS times,
    then log the final failure and return False. Never raises."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def refuse(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
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
    assert call_count == _wc._PUBLISH_MAX_ATTEMPTS
    # Final log line should reference the attempt count so operators can
    # distinguish "single blip" from "extended outage".
    joined = " ".join(r.message for r in caplog.records)
    assert f"after {_wc._PUBLISH_MAX_ATTEMPTS} attempts" in joined


def test_publish_priority_retries_on_5xx_then_gives_up(monkeypatch, caplog):
    """5xx is server-side transient — retry within budget."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def five_hundred(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, text="worklist-api restarting")

    transport = httpx.MockTransport(five_hundred)
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://worklist-api:8107",
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="STAT", priority_score=95,
        ))
    assert ok is False
    assert call_count == _wc._PUBLISH_MAX_ATTEMPTS


def test_publish_priority_does_NOT_retry_on_4xx(monkeypatch, caplog):
    """4xx = caller-side bug. The same bad payload will 422 forever, so retrying
    just wastes latency budget. Try once, log, give up."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def bad_request(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(422, text='{"detail":"bad tier"}')

    transport = httpx.MockTransport(bad_request)
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://worklist-api:8107",
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="BADTIER", priority_score=999,
        ))
    assert ok is False
    assert call_count == 1  # exactly one attempt; no retries
    joined = " ".join(r.message for r in caplog.records)
    assert "no retry" in joined  # log spells it out for operators


def test_publish_priority_recovers_after_transient_blip(monkeypatch):
    """The whole point of the retry: a single transient blip self-heals.
    Verify that if the first attempt fails but the second succeeds, we get
    True back — the study's priority makes it to the store."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("blip", request=request)
        return httpx.Response(204)

    transport = httpx.MockTransport(flaky)
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="STAT", priority_score=95,
    ))
    assert ok is True
    assert call_count == 2  # recovered on the second attempt


def test_publish_priority_recovers_after_5xx_blip(monkeypatch):
    """Same recovery contract for a 5xx that clears on retry — the Worklist API
    briefly restarts, then succeeds. Priority still makes it through."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(502)
        return httpx.Response(204)

    transport = httpx.MockTransport(flaky)
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="URGENT", priority_score=70,
    ))
    assert ok is True
    assert call_count == 2


def test_publish_priority_swallows_malformed_url_never_raises(monkeypatch, caplog):
    """A malformed WORKLIST_API_URL (here: an invalid port) makes httpx raise
    httpx.InvalidURL, which is NOT an httpx.HTTPError. The helper must still swallow
    it and return False. If it escaped, the publish activity would fail, and under
    its unbounded Temporal retry (the same URL fails identically every time) that
    would wedge the study at READY_FOR_READ forever. No network call is made and
    there is no retry: URL parsing fails before the transport is ever reached."""
    _zero_backoff(monkeypatch)  # prove the failure is NOT the retry path sleeping
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    _install(monkeypatch, transport)
    with caplog.at_level("WARNING"):
        ok = _run(publish_priority(
            "http://[::bad",  # unparseable: invalid port -> httpx.InvalidURL
            study_instance_uid="1.2.3", workflow_id="wf_1",
            priority_tier="STAT", priority_score=95,
        ))
    assert ok is False
    assert calls["n"] == 0  # never reached the transport; no wasted attempts
    joined = " ".join(r.message for r in caplog.records)
    assert "no retry" in joined
    assert "wf_1" in joined


def test_publish_priority_swallows_unusable_scheme_without_retry(monkeypatch):
    """A base_url with no/unknown scheme raises httpx.UnsupportedProtocol. Like a
    malformed URL it is a permanent config error, so the helper gives up after one
    attempt rather than burning the full retry budget on a URL that can never work."""
    _zero_backoff(monkeypatch)
    ok = _run(publish_priority(
        "worklist-api:8107",  # missing scheme -> httpx.UnsupportedProtocol
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="STAT", priority_score=95,
    ))
    assert ok is False  # swallowed, never raised


def test_publish_priority_success_first_try_makes_only_one_call(monkeypatch):
    """Sanity: happy path should NOT retry; a successful publish must be one
    HTTP call, not the full attempt budget."""
    _zero_backoff(monkeypatch)
    call_count = 0

    def ok_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(204)

    transport = httpx.MockTransport(ok_handler)
    _install(monkeypatch, transport)
    ok = _run(publish_priority(
        "http://worklist-api:8107",
        study_instance_uid="1.2.3", workflow_id="wf_1",
        priority_tier="ROUTINE", priority_score=50,
    ))
    assert ok is True
    assert call_count == 1  # exactly one; no wasted attempts
