"""RIS event nudge: real-time sign-off detection without giving up the poll fallback (#25).

The RIS side (OpenMRS module hook / Atomfeed bridge) POSTs to /webhooks/ris/event and the
poller's next sweep happens immediately instead of after up to POLL_INTERVAL_S. The nudge is
data-free: correctness still comes from the fhir2 cursor sweep, so a lost, duplicated, or
spurious nudge changes nothing (non-breaking; polling retained as the fallback).

Skipped when the orchestrator's deps aren't installed.
"""
from __future__ import annotations

import asyncio
import time

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")


@pytest.fixture(autouse=True)
def _fresh_wake():
    ingress._WAKE = None
    yield
    ingress._WAKE = None


def test_nudge_wakes_the_wait_immediately():
    async def scenario():
        ingress._wake_event().set()          # a nudge arrived (possibly during the last sweep)
        start = time.monotonic()
        nudged = await ingress._sleep_or_nudge(30.0)
        return nudged, time.monotonic() - start

    nudged, elapsed = asyncio.run(scenario())
    assert nudged is True
    assert elapsed < 1.0  # did not wait out the 30s interval


def test_without_a_nudge_the_wait_times_out_to_the_normal_poll():
    async def scenario():
        return await ingress._sleep_or_nudge(0.05)  # tiny interval stands in for 30s

    assert asyncio.run(scenario()) is False  # fallback path: plain interval polling


def test_nudge_burst_coalesces_into_one_immediate_sweep():
    async def scenario():
        for _ in range(5):                   # RIS fires a burst of events
            ingress._wake_event().set()
        first = await ingress._sleep_or_nudge(30.0)
        second = await ingress._sleep_or_nudge(0.05)
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True    # the burst produced exactly one immediate sweep...
    assert second is False  # ...and was fully consumed: the next wait is a normal interval


def test_nudge_landing_mid_sweep_is_not_lost():
    async def scenario():
        # No nudge pending when the sweep starts; one lands while it "runs".
        ingress._wake_event().set()          # (the event stays set until the next wait consumes it)
        return await ingress._sleep_or_nudge(30.0)

    assert asyncio.run(scenario()) is True


def test_webhook_sets_the_wake_event_and_needs_no_payload():
    result = asyncio.run(ingress.ris_event())
    assert result == {"nudged": True}
    assert ingress._wake_event().is_set()    # the poller's next wait returns immediately
