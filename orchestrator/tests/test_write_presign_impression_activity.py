"""write_presign_impression_activity: the real activity body (#26).

The workflow-level test in test_presign_impression.py mocks this activity out, so it never runs
the actual body. This file covers the body directly: it calls Fhir2Client.write_presign_impression
and returns the report id, and it lets errors propagate so the workflow's bounded retry and
skip-on-failure path can run (a fhir2 write outage must never strand the human read).
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("temporalio", reason="orchestrator deps not installed")

from orchestrator import activities  # noqa: E402


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_activity_calls_fhir_client_and_returns_report_id(monkeypatch):
    seen: dict = {}

    class _Recording:
        def __init__(self, *a, **kw):
            pass

        async def write_presign_impression(self, service_request_ref, patient_ref, impression_text):
            seen["args"] = (service_request_ref, patient_ref, impression_text)
            return "draft-42"

    monkeypatch.setattr(activities, "Fhir2Client", _Recording)
    report_id = _run(activities.write_presign_impression_activity(
        "ServiceRequest/sr-1", "Patient/pat-1", "No acute findings identified.",
    ))
    assert report_id == "draft-42"
    assert seen["args"] == ("ServiceRequest/sr-1", "Patient/pat-1", "No acute findings identified.")


def test_activity_propagates_fhir_error_for_the_workflow_to_handle(monkeypatch):
    """The activity must NOT swallow errors. The workflow relies on the ActivityError to run its
    bounded retry and then skip the draft, so a fhir2 write failure never strands the read."""
    class _Failing:
        def __init__(self, *a, **kw):
            pass

        async def write_presign_impression(self, *a, **kw):
            raise RuntimeError("fhir2 write down")

    monkeypatch.setattr(activities, "Fhir2Client", _Failing)
    with pytest.raises(RuntimeError):
        _run(activities.write_presign_impression_activity(
            "ServiceRequest/sr-1", "Patient/pat-1", "text",
        ))
