"""Tests for POST /events/ohif-opened (#73 item 1).

Uses FastAPI's TestClient directly rather than the WorkflowEnvironment approach in
test_ingress_idempotent.py: this endpoint is intentionally decoupled from Temporal (no
workflow signal, no store persistence beyond a log line), so a plain HTTP test hits its
whole behavior. Same importorskip pattern as the other ingress tests so the suite still
runs cleanly when orchestrator deps aren't installed in the current lane.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("temporalio", reason="orchestrator deps not installed")
from fastapi.testclient import TestClient  # noqa: E402

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from orchestrator.ingress import app  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _valid_event() -> dict:
    """A payload matching contracts/events/ohif-opened.schema.json."""
    return {
        "schemaVersion": "1.0.0",
        "eventType": "ohif.study.opened",
        "studyInstanceUID": "1.2.840.113619.2.55.3.111.999",
        "openedAt": datetime.now(timezone.utc).isoformat(),
        "radiologistId": "rad-42",
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_valid_event_returns_202(client):
    """The primary contract: a schema-valid ohif.study.opened event is accepted
    and returns 202 with the studyInstanceUID echoed for correlation."""
    body = _valid_event()
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 202
    assert resp.json() == {
        "accepted": True,
        "studyInstanceUID": body["studyInstanceUID"],
    }


def test_valid_event_without_optional_radiologistId(client):
    """radiologistId is optional in the schema; a payload without it is still 202.
    Rationale: a viewer opened without a signed-in radiologist (rare but possible in a
    demo or QA context) still records that a study was opened."""
    body = _valid_event()
    del body["radiologistId"]
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# Schema failures -> 400
# ---------------------------------------------------------------------------
def test_missing_required_field_returns_400(client):
    """studyInstanceUID is required by the schema; missing it -> 400 with a
    schema-error detail. The producer (extension) treats this as a warning, not
    a UI-blocking error."""
    body = _valid_event()
    del body["studyInstanceUID"]
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 400
    assert "schema" in resp.json()["detail"]


def test_wrong_event_type_returns_400(client):
    """eventType is `const` in the schema; anything other than
    "ohif.study.opened" is a producer bug and gets 400."""
    body = _valid_event()
    body["eventType"] = "ohif.study.closed"  # not our contract
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 400


def test_extra_field_returns_400(client):
    """The schema is `additionalProperties: false`. An unknown field is a
    producer/consumer contract drift signal and is rejected loudly so the
    drift shows up in the extension's console rather than being silently
    dropped in the ingress."""
    body = _valid_event()
    body["extraField"] = "surprise"
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 400


def test_malformed_schemaVersion_returns_400(client):
    """schemaVersion follows a semver-shaped pattern in the schema; a
    non-conforming value -> 400."""
    body = _valid_event()
    body["schemaVersion"] = "not-a-version"
    resp = client.post("/events/ohif-opened", json=body)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Logging: the acceptance criterion is "recorded by the ingress (visible in logs/store)"
# ---------------------------------------------------------------------------
def test_valid_event_logs_at_info(client, caplog):
    """The endpoint's acceptance criterion in #73 is "visible in logs/store".
    The endpoint logs a structured line at INFO with studyInstanceUID + radiologistId + openedAt.
    Store persistence is intentionally deferred (see the endpoint docstring); log capture
    satisfies the acceptance criterion for the demo."""
    body = _valid_event()
    with caplog.at_level("INFO", logger="orchestrator.ingress.ohif"):
        client.post("/events/ohif-opened", json=body)
    assert any(
        "ohif.study.opened accepted" in r.message
        and body["studyInstanceUID"] in r.message
        for r in caplog.records
    )
