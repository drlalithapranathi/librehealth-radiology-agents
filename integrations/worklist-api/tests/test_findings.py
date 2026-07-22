"""Tests for the AI findings surface on the Worklist API (#89).

Covers the store + the router as one integration slice; the router thin-wraps the store,
so testing them together catches contract-level bugs the store's own tests would miss
(e.g., pydantic filtering out extra fields silently, 404 shape on missing entries).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Allow imports from the worklist-api package directory the same way conftest.py does
sys.path.insert(0, str(Path(__file__).parent.parent))

from findings import create_findings_router  # noqa: E402
from findings_store import FindingsStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def store() -> FindingsStore:
    return FindingsStore(":memory:")


@pytest.fixture
def client(store: FindingsStore) -> TestClient:
    app = FastAPI()
    app.include_router(create_findings_router(store))
    return TestClient(app)


def _valid_findings_payload(**overrides) -> dict:
    """A payload matching FindingsPublish. Direct passthrough of interpretation.runTools output."""
    base = {
        "studyInstanceUID": "1.2.840.113619.2.55.3.111.999",
        "workflowId": "wf_test_123",
        "findings": [
            {
                "toolId": "pneumothorax-detect",
                "label": "Pneumothorax (screening p=0.87)",
                "confidence": 0.87,
                "evidenceRef": "orthanc:instance/inst-xyz",
                "status": "COMPLETE",
            },
            {
                "toolId": "pe-detect",
                "label": "",
                "confidence": None,
                "evidenceRef": None,
                "status": "STUBBED",
            },
        ],
        "overallStatus": "PARTIAL",
        "generatedAt": "2026-07-21T12:00:00Z",
    }
    base.update(overrides)
    return base


# ===========================================================================
# Store: raw put/get behavior
# ===========================================================================
def test_store_put_then_get_round_trips(store):
    """Basic write + read. Findings JSON is round-tripped byte-for-byte on the wire (list
    of dicts in → list of dicts out) so downstream consumers can trust the shape they get
    back matches what interpretation.runTools emitted."""
    store.put(
        study_instance_uid="uid-1",
        workflow_id="wf-1",
        findings=[{"toolId": "t", "label": "l", "confidence": 0.5,
                   "evidenceRef": "orthanc:instance/x", "status": "COMPLETE"}],
        overall_status="COMPLETE",
        generated_at="2026-07-21T12:00:00Z",
        updated_at="2026-07-21T12:00:01Z",
    )
    got = store.get("uid-1")
    assert got is not None
    assert got["studyInstanceUID"] == "uid-1"
    assert got["workflowId"] == "wf-1"
    assert got["overallStatus"] == "COMPLETE"
    assert len(got["findings"]) == 1
    assert got["findings"][0]["toolId"] == "t"


def test_store_get_missing_returns_none(store):
    """Missing study → None (not empty dict, not exception). The endpoint layer converts this
    to a 404 so the OHIF extension can distinguish "not published yet" from "published as
    empty"."""
    assert store.get("does-not-exist") is None


def test_store_put_upserts_on_conflict(store):
    """Re-publishing overwrites the prior findings. Rationale: the workflow is the source of
    truth for interpretation output; if it re-runs (rare — a workflow rerun with a code
    change), the current findings are what OHIF should show."""
    store.put(
        study_instance_uid="uid-1", workflow_id="wf-1",
        findings=[{"toolId": "old", "label": "", "confidence": None,
                   "evidenceRef": None, "status": "STUBBED"}],
        overall_status="STUBBED", generated_at="t1", updated_at="t1",
    )
    store.put(
        study_instance_uid="uid-1", workflow_id="wf-1",
        findings=[{"toolId": "new", "label": "Positive", "confidence": 0.9,
                   "evidenceRef": "orthanc:instance/x", "status": "COMPLETE"}],
        overall_status="COMPLETE", generated_at="t2", updated_at="t2",
    )
    got = store.get("uid-1")
    assert got["findings"][0]["toolId"] == "new"
    assert got["overallStatus"] == "COMPLETE"


# ===========================================================================
# Router: POST + GET happy path
# ===========================================================================
def test_post_valid_findings_returns_204(client, store):
    """Orchestrator publishes → 204 No Content (mirrors /priority for consistency). No body
    on the response; the publish is fire-and-forget from the workflow's perspective."""
    resp = client.post("/findings", json=_valid_findings_payload())
    assert resp.status_code == 204
    # And the store now has the entry
    assert store.get("1.2.840.113619.2.55.3.111.999") is not None


def test_get_existing_study_returns_findings(client):
    """Full round-trip through the endpoint: post → get returns the stored shape."""
    payload = _valid_findings_payload()
    client.post("/findings", json=payload)
    resp = client.get(f"/findings/{payload['studyInstanceUID']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["studyInstanceUID"] == payload["studyInstanceUID"]
    assert body["workflowId"] == payload["workflowId"]
    assert body["overallStatus"] == "PARTIAL"
    assert len(body["findings"]) == 2
    # updatedAt is populated by the endpoint (server-side clock); generatedAt is what the
    # orchestrator sent. Contract: the extension uses generatedAt for staleness, updatedAt
    # for "did the endpoint see this recently" diagnostics.
    assert body["generatedAt"] == payload["generatedAt"]
    assert "updatedAt" in body


# ===========================================================================
# Router: 404 for not-yet-published
# ===========================================================================
def test_get_missing_study_returns_404(client):
    """Study opened in OHIF before the workflow's interpretation completes → 404 with a
    clear detail. The extension distinguishes this from "published empty" (200 with
    overallStatus STUBBED) so it can show a subdued "AI still analyzing" hint rather than
    a false "no findings" claim."""
    resp = client.get("/findings/never-published")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no findings yet"}


# ===========================================================================
# Router: schema failure modes
# ===========================================================================
def test_post_missing_required_field_returns_422(client):
    """studyInstanceUID is required; missing it → 422 (pydantic default). The publisher
    treats this as a permanent caller-side bug (no retry)."""
    bad = _valid_findings_payload()
    del bad["studyInstanceUID"]
    resp = client.post("/findings", json=bad)
    assert resp.status_code == 422


def test_post_extra_fields_are_ignored(client, store):
    """The FindingsPublish model uses `extra = "ignore"` so schema evolution on
    interpretation.runTools (adding new finding fields) doesn't 422 this endpoint. The
    stored payload keeps only the known fields."""
    payload = _valid_findings_payload()
    payload["someNewFieldFromFutureInterpretationVersion"] = "harmless"
    payload["findings"][0]["boundingBox"] = [10, 20, 30, 40]  # future spatial evidence
    resp = client.post("/findings", json=payload)
    assert resp.status_code == 204
    # Round-trip: the stored findings drop the unknown field but keep known ones.
    got = store.get(payload["studyInstanceUID"])
    assert "boundingBox" not in got["findings"][0]  # unknown, filtered
    assert got["findings"][0]["status"] == "COMPLETE"  # known, kept


# ===========================================================================
# Semantics: empty findings vs missing study
# ===========================================================================
def test_get_returns_200_with_empty_findings_when_published_but_all_stubbed(client):
    """A study where the workflow ran but every tool returned STUBBED → 200 with the
    stubbed findings visible. OHIF's rendering rules make these silent, not the endpoint's
    job. Distinct from a 404, which is "haven't heard from the workflow yet"."""
    payload = _valid_findings_payload(overallStatus="STUBBED")
    payload["findings"] = [
        {"toolId": "pe-detect", "label": "", "confidence": None,
         "evidenceRef": None, "status": "STUBBED"},
    ]
    client.post("/findings", json=payload)
    resp = client.get(f"/findings/{payload['studyInstanceUID']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["overallStatus"] == "STUBBED"
    assert body["findings"][0]["status"] == "STUBBED"
