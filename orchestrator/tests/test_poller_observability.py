"""Unit tests for the RIS poller's failure observability (issue #53).

The poller deliberately swallows fhir2 errors to keep the loop alive, but a persistent outage
(fhir2 down, credentials rejected) must be VISIBLE: before #53 a failing poll left zero log
evidence while sign-offs went undetected. Covers: warn on first failure, throttle to every Nth
after that, and a recovery line when polling resumes. Skipped when the orchestrator's deps
(temporalio/fastapi) aren't installed.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")


@pytest.fixture(autouse=True)
def _fresh_store():
    """Each test gets an isolated in-memory durable store (#6 replaced the in-process dict)."""
    ingress._STORE = ingress.IngressStore(":memory:")
    yield
    ingress._STORE.close()
    ingress._STORE = None


@pytest.fixture(autouse=True)
def _no_temporal(monkeypatch):
    """The loop's reconciliation path must not reach for a live Temporal server."""
    async def fake_temporal():
        return None

    async def fake_reconcile(client):
        return 0

    monkeypatch.setattr(ingress, "_temporal", fake_temporal)
    monkeypatch.setattr(ingress, "_reconcile_index", fake_reconcile)


def _run_poller_for(iterations, monkeypatch):
    """Run _ris_poller with a no-op wait that cancels the loop after `iterations` polls.

    The loop's per-iteration wait is `_sleep_or_nudge` (the RIS event nudge, #25), not a bare
    `asyncio.sleep`, so the drive hook patches that — patching `asyncio.sleep` would no longer
    intercept the loop and the poller would block on the real 30s wait forever."""
    calls = 0

    async def counting_wait(_seconds):
        nonlocal calls
        calls += 1
        if calls > iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(ingress, "_sleep_or_nudge", counting_wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ingress._ris_poller())


def test_failing_polls_warn_first_then_throttled(monkeypatch, caplog):
    async def failing_poll(cursor):
        raise RuntimeError("simulated fhir2 401")

    monkeypatch.setattr(ingress.activities, "poll_finalized_reports", failing_poll)
    with caplog.at_level(logging.WARNING, logger="orchestrator.ingress"):
        _run_poller_for(25, monkeypatch)

    stalled = [r for r in caplog.records if "sign-off detection is stalled" in r.getMessage()]
    # 25 consecutive failures, FAILED_POLLS_PER_WARNING=10 -> warned at #1, #10, #20 only:
    # visible immediately, throttled thereafter, never silent.
    assert [r.getMessage() for r in stalled] == [
        "fhir2 poll failed (1 consecutive); sign-off detection is stalled",
        "fhir2 poll failed (10 consecutive); sign-off detection is stalled",
        "fhir2 poll failed (20 consecutive); sign-off detection is stalled",
    ]


def test_recovery_is_logged_and_counter_resets(monkeypatch, caplog):
    outcomes = iter([Exception, Exception, None, None])

    async def flaky_poll(cursor):
        outcome = next(outcomes)
        if outcome is Exception:
            raise RuntimeError("simulated fhir2 outage")
        return [], None

    monkeypatch.setattr(ingress.activities, "poll_finalized_reports", flaky_poll)
    with caplog.at_level(logging.WARNING, logger="orchestrator.ingress"):
        _run_poller_for(4, monkeypatch)

    messages = [r.getMessage() for r in caplog.records]
    assert "fhir2 poll recovered after 2 consecutive failure(s)" in messages
    # The recovery line appears exactly once: the counter reset, so the second healthy poll
    # must not re-announce it.
    assert messages.count("fhir2 poll recovered after 2 consecutive failure(s)") == 1
