"""publish_priority_activity — wiring behavior.

Directly invokes the activity function (not via Temporal) with a monkey-patched
worklist_client to verify:
  * happy path calls the helper with the right payload and returns None
  * missing tier/score skips the helper call (logs and returns), matching the
    "publish is visibility, not correctness" invariant that must never fail a workflow
  * a helper-level failure (helper returns False) is silently swallowed by the
    activity — the workflow proceeds regardless
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("temporalio", reason="orchestrator deps not installed")

from orchestrator import activities  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_activity_calls_helper_with_expected_payload(monkeypatch):
    called_with: dict = {}

    async def fake_publish(base_url, *, study_instance_uid, workflow_id,
                            priority_tier, priority_score):
        called_with.update({
            "base_url": base_url, "study": study_instance_uid, "wf": workflow_id,
            "tier": priority_tier, "score": priority_score,
        })
        return True

    monkeypatch.setattr(activities, "publish_priority_to_worklist", fake_publish)
    triage = {"priorityTier": "STAT", "priorityScore": 95, "rationale": ["..."]}
    result = _run(activities.publish_priority_activity(
        "wf_1", "1.2.3", triage,
    ))
    assert result is None                    # activity returns None (no workflow signal)
    assert called_with["wf"] == "wf_1"
    assert called_with["study"] == "1.2.3"
    assert called_with["tier"] == "STAT"
    assert called_with["score"] == 95


def test_activity_skips_helper_when_triage_is_missing_fields(monkeypatch, caplog):
    """If the triage output is malformed (no tier/score) the activity must not attempt
    a doomed publish. Log at WARNING and return; the workflow proceeds."""
    called = False

    async def fake_publish(*a, **kw):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(activities, "publish_priority_to_worklist", fake_publish)
    with caplog.at_level(logging.WARNING):
        _run(activities.publish_priority_activity(
            "wf_1", "1.2.3", {"rationale": ["no tier here"]},
        ))
    assert called is False
    joined = " ".join(r.message for r in caplog.records)
    assert "publish priority skipped" in joined


def test_activity_swallows_helper_failure(monkeypatch):
    """Even if the helper returns False (Worklist API down), the activity must
    still return None cleanly — the workflow relies on this to stay running."""
    async def fake_publish(*a, **kw):
        return False

    monkeypatch.setattr(activities, "publish_priority_to_worklist", fake_publish)
    result = _run(activities.publish_priority_activity(
        "wf_1", "1.2.3",
        {"priorityTier": "URGENT", "priorityScore": 70},
    ))
    assert result is None


def test_activity_uses_worklist_api_base_url_from_state(monkeypatch):
    """The base URL comes from state.worklist_api_base_url — that's the seam a
    deploy points at a different Worklist API instance."""
    monkeypatch.setenv("WORKLIST_API_URL", "http://custom-worklist:9999")
    seen: dict = {}

    async def fake_publish(base_url, **kw):
        seen["base_url"] = base_url
        return True

    monkeypatch.setattr(activities, "publish_priority_to_worklist", fake_publish)
    _run(activities.publish_priority_activity(
        "wf_1", "1.2.3",
        {"priorityTier": "ROUTINE", "priorityScore": 50},
    ))
    assert seen["base_url"] == "http://custom-worklist:9999"
