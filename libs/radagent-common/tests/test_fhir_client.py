"""Unit tests for Fhir2Client.poll_finalized_reports (issue #12).

Mocks the fhir2 HTTP layer (no live server) and checks: the query pages by INCLUSIVE `_lastUpdated`
(NOT the `status` param, which 400s on live fhir2), `status == final` is filtered client-side,
Bundle `next` pages are followed, and the high-water cursor is the max lastUpdated across ALL
entries. Each report is projected to a lean, PHI-free record.
"""
from __future__ import annotations

import asyncio

from radagent_common.fhir_client import Fhir2Client, finalized_report_record

_FINAL = {
    "resourceType": "DiagnosticReport", "id": "rep-1", "status": "final",
    "basedOn": [{"reference": "ServiceRequest/sr-1"}],
    "identifier": [{"type": {"coding": [{"code": "ACSN"}]}, "value": "ACC-CXR-001"}],
    "issued": "2026-06-27T12:30:00Z",
    "meta": {"lastUpdated": "2026-06-27T12:30:05Z"},
}
_PRELIM = {
    "resourceType": "DiagnosticReport", "id": "rep-2", "status": "preliminary",
    "meta": {"lastUpdated": "2026-06-27T12:31:00Z"},  # newest overall, but NOT final
}
_FINAL2 = {
    "resourceType": "DiagnosticReport", "id": "rep-3", "status": "final",
    "meta": {"lastUpdated": "2026-06-27T12:40:00Z"},
}


def _bundle(*resources, next_url=None):
    b = {"resourceType": "Bundle", "type": "searchset",
         "entry": [{"resource": r} for r in resources]}
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


def test_poll_uses_inclusive_ge_filters_final_and_reports_high_water():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(_FINAL, _PRELIM)

    client._get = fake_get  # type: ignore[assignment]
    reports, high_water = asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z"))

    # Inclusive ge (NOT gt, NOT status=final) so a same-second report is never lost.
    assert calls[0] == ("DiagnosticReport",
                        {"_lastUpdated": "ge2026-06-27T00:00:00Z", "_sort": "_lastUpdated"})
    # Only the finalized report survives the client-side status filter.
    assert [r["diagnosticReportId"] for r in reports] == ["DiagnosticReport/rep-1"]
    assert reports[0]["serviceRequestRef"] == "ServiceRequest/sr-1"
    assert reports[0]["accessionNumber"] == "ACC-CXR-001"
    # High-water = max lastUpdated across ALL entries (incl. the non-final one) so the poller
    # advances past non-final reports instead of stalling.
    assert high_water == "2026-06-27T12:31:00Z"
    assert set(reports[0]) == {"diagnosticReportId", "status", "serviceRequestRef",
                              "accessionNumber", "signedAt", "lastUpdatedCursor"}


def test_poll_follows_bundle_next_link():
    client = Fhir2Client()
    page1 = _bundle(_PRELIM, next_url="http://fhir/DiagnosticReport?page=2")  # page 1: no final
    page2 = _bundle(_FINAL2)
    responses = [page1, page2]
    paths = []

    async def fake_get(path, params=None):
        paths.append(path)
        return responses.pop(0)

    client._get = fake_get  # type: ignore[assignment]
    reports, high_water = asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z"))

    # The final report on page 2 is collected — not dropped by reading only page 1.
    assert [r["diagnosticReportId"] for r in reports] == ["DiagnosticReport/rep-3"]
    # Page 2 was fetched by its absolute next URL.
    assert paths == ["DiagnosticReport", "http://fhir/DiagnosticReport?page=2"]
    assert high_water == "2026-06-27T12:40:00Z"


def test_poll_empty_bundle():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return {"resourceType": "Bundle", "type": "searchset"}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.poll_finalized_reports("2026-06-27T00:00:00Z")) == ([], None)


def test_finalized_record_tolerates_missing_refs():
    rec = finalized_report_record({"id": "x", "status": "final"})
    assert rec["diagnosticReportId"] == "DiagnosticReport/x"
    assert rec["serviceRequestRef"] is None
    assert rec["accessionNumber"] is None
    assert rec["lastUpdatedCursor"] is None


# --- resolve_order_by_accession (issue #11) --------------------------------
_SR = {
    "resourceType": "ServiceRequest", "id": "sr-9",
    "subject": {"reference": "Patient/pat-9"},
    "identifier": [{"type": {"coding": [{"code": "ACSN"}]}, "value": "ACC-9"}],
}


def test_resolve_by_accession_returns_patient_and_order_refs():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(_SR)

    client._get = fake_get  # type: ignore[assignment]
    resolved = asyncio.run(client.resolve_order_by_accession("ACC-9"))
    # Searches ServiceRequest by its identifier (the accession) -- not by status, which 400s.
    assert calls[0] == ("ServiceRequest", {"identifier": "ACC-9"})
    assert resolved == {"fhirPatientId": "Patient/pat-9",
                        "fhirServiceRequestId": "ServiceRequest/sr-9"}


def test_resolve_by_accession_none_on_miss():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return {"resourceType": "Bundle", "type": "searchset"}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("NOPE")) is None


def test_resolve_by_accession_skips_serviceRequest_without_subject():
    client = Fhir2Client()
    partial = {"resourceType": "ServiceRequest", "id": "sr-x"}  # no subject -> not resolvable

    async def fake_get(path, params=None):
        return _bundle(partial)

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("ACC-X")) is None


def test_resolve_by_accession_empty_accession_makes_no_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.resolve_order_by_accession("")) is None
    assert called is False  # empty accession short-circuits, no fhir2 round-trip


# --- get_report_conclusion (issue #16) -------------------------------------
def test_get_report_conclusion_reads_the_typed_ref():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1",
                "conclusion": "Large left pneumothorax."}

    client._get = fake_get  # type: ignore[assignment]
    text = asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1"))
    # A prefixed ref is fetched as-is; a bare id would be prefixed with DiagnosticReport/.
    assert calls == ["DiagnosticReport/rep-1"]
    assert text == "Large left pneumothorax."


def test_get_report_conclusion_prefixes_a_bare_id():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "DiagnosticReport", "id": "rep-1", "conclusion": "x"}

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.get_report_conclusion("rep-1"))
    assert calls == ["DiagnosticReport/rep-1"]


def test_get_report_conclusion_none_when_absent_or_blank():
    client = Fhir2Client()

    async def no_conclusion(path, params=None):
        return {"resourceType": "DiagnosticReport", "id": "rep-1"}  # no conclusion field

    client._get = no_conclusion  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1")) is None

    async def blank_conclusion(path, params=None):
        return {"resourceType": "DiagnosticReport", "conclusion": "   "}

    client._get = blank_conclusion  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("DiagnosticReport/rep-1")) is None


def test_get_report_conclusion_empty_id_makes_no_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return {}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.get_report_conclusion("")) is None
    assert called is False  # empty id short-circuits, no fhir2 round-trip


# ---- basic auth from env (issue #53) ----------------------------------------------

def test_auth_from_env_and_passed_to_httpx(monkeypatch):
    monkeypatch.setenv("FHIR2_BASIC_USER", "poller")
    monkeypatch.setenv("FHIR2_BASIC_PASS", "s3cret")
    client = Fhir2Client()
    assert client._auth == ("poller", "s3cret")

    # The credential must reach the HTTP layer: live fhir2 401s every unauthenticated read,
    # and callers swallow fhir2 errors, so a dropped credential fails silently (#53).
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"resourceType": "Bundle"}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    import radagent_common.fhir_client as fhir_client_module
    monkeypatch.setattr(fhir_client_module.httpx, "AsyncClient", _FakeClient)
    asyncio.run(client._get("DiagnosticReport"))
    assert captured["auth"] == ("poller", "s3cret")


def test_no_env_stays_unauthenticated(monkeypatch):
    monkeypatch.delenv("FHIR2_BASIC_USER", raising=False)
    monkeypatch.delenv("FHIR2_BASIC_PASS", raising=False)
    assert Fhir2Client()._auth is None  # mocks and unit tests keep working with no env set


def test_half_set_credentials_fail_loudly(monkeypatch):
    """A partial pair silently downgrading to unauthenticated would recreate the silent-401
    disease #53 exists to cure — reject it at construction instead."""
    import pytest

    monkeypatch.setenv("FHIR2_BASIC_USER", "poller")
    monkeypatch.delenv("FHIR2_BASIC_PASS", raising=False)
    with pytest.raises(ValueError):
        Fhir2Client()
