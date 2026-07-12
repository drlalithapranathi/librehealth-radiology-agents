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


# --- write_presign_impression (issue #26) ----------------------------------

def test_write_presign_impression_creates_when_no_existing_draft():
    client = Fhir2Client()
    get_calls = []
    post_calls = []

    async def fake_get(path, params=None):
        get_calls.append((path, params))
        return _bundle()  # no existing preliminary draft for this order

    async def fake_post(path, resource):
        post_calls.append((path, resource))
        return {"id": "new-draft-1"}

    client._get = fake_get  # type: ignore[assignment]
    client._post = fake_post  # type: ignore[assignment]
    report_id = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/pat-1", "No acute findings identified."))

    # Searches by `based-on` only -- NOT `status`, which 400s on live fhir2 (#3 spike).
    assert get_calls == [("DiagnosticReport", {"based-on": "ServiceRequest/sr-1"})]
    assert post_calls[0][0] == "DiagnosticReport"
    posted = post_calls[0][1]
    assert posted["status"] == "preliminary"
    assert posted["subject"] == {"reference": "Patient/pat-1"}
    assert posted["basedOn"] == [{"reference": "ServiceRequest/sr-1"}]
    assert posted["conclusion"] == "No acute findings identified."
    assert "id" not in posted  # a create, not an update
    assert report_id == "new-draft-1"


def test_write_presign_impression_updates_the_existing_draft():
    client = Fhir2Client()
    put_calls = []

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "DiagnosticReport", "id": "draft-9", "status": "preliminary"})

    async def fake_put(path, resource):
        put_calls.append((path, resource))
        return {"id": "draft-9"}

    async def fail_post(path, resource):
        raise AssertionError("must update the existing draft, not create a new one")

    client._get = fake_get  # type: ignore[assignment]
    client._put = fake_put  # type: ignore[assignment]
    client._post = fail_post  # type: ignore[assignment]
    report_id = asyncio.run(client.write_presign_impression(
        "ServiceRequest/sr-1", "Patient/pat-1", "Findings consistent with pneumothorax."))

    assert put_calls[0][0] == "DiagnosticReport/draft-9"
    assert put_calls[0][1]["id"] == "draft-9"
    assert put_calls[0][1]["conclusion"] == "Findings consistent with pneumothorax."
    assert report_id == "draft-9"


def test_find_presign_draft_ignores_non_preliminary_reports():
    """A prior order can carry a `final` DiagnosticReport (a signed report from a prior study,
    or even this one already signed) -- only a `preliminary` one is this draft's idempotency key."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle(
            {"resourceType": "DiagnosticReport", "id": "final-1", "status": "final"},
            {"resourceType": "DiagnosticReport", "id": "draft-2", "status": "preliminary"},
        )

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client._find_presign_draft("ServiceRequest/sr-1")) == "draft-2"


def test_find_presign_draft_none_when_no_preliminary_report():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "DiagnosticReport", "id": "final-1", "status": "final"})

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client._find_presign_draft("ServiceRequest/sr-1")) is None


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
# --- EHR read helpers (issue #4) ---------------------------------------------

def test_get_patient_accepts_bare_id_and_reference():
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"resourceType": "Patient", "id": "demo-1", "gender": "female"}

    client._get = fake_get  # type: ignore[assignment]
    asyncio.run(client.get_patient("demo-1"))
    asyncio.run(client.get_patient("Patient/demo-1"))
    # Bare id gets normalised to the reference form; a full ref passes through as-is.
    assert calls == ["Patient/demo-1", "Patient/demo-1"]


def test_get_patient_none_on_empty_id():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return {}

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.get_patient("")) is None
    assert called is False


def test_get_patient_none_on_404():
    import httpx as _httpx
    client = Fhir2Client()

    async def raise_404(path, params=None):
        raise _httpx.HTTPStatusError(
            "not found", request=_httpx.Request("GET", "http://x"),
            response=_httpx.Response(404))

    client._get = raise_404  # type: ignore[assignment]
    assert asyncio.run(client.get_patient("Patient/nope")) is None


# --- search_imaging_studies --------------------------------------------------

def test_search_imaging_studies_projects_to_lean_priors():
    client = Fhir2Client()
    calls = []
    resource = {
        "resourceType": "ImagingStudy", "id": "img-1",
        "started": "2025-04-12T00:00:00Z",
        "modality": [{"system": "http://dicom.nema.org/resources/ontology/DCM", "code": "CT"}],
    }

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(resource)

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("Patient/demo-1"))
    # patient search param uses the BARE id, not the reference form (some OpenMRS
    # builds reject 'Patient/demo-1' on `patient=` — see _patient_query).
    assert calls[0] == ("ImagingStudy", {"patient": "demo-1"})
    assert priors == [{"ref": "ImagingStudy/img-1", "modality": "CT", "date": "2025-04-12T00:00:00Z"}]


def test_search_imaging_studies_follows_bundle_next_link():
    client = Fhir2Client()
    r1 = {"resourceType": "ImagingStudy", "id": "img-1"}
    r2 = {"resourceType": "ImagingStudy", "id": "img-2"}
    responses = [_bundle(r1, next_url="http://fhir/ImagingStudy?page=2"), _bundle(r2)]

    async def fake_get(path, params=None):
        return responses.pop(0)

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("demo-1"))
    assert [p["ref"] for p in priors] == ["ImagingStudy/img-1", "ImagingStudy/img-2"]


def test_search_imaging_studies_tolerates_missing_modality_and_date():
    """Some scanners omit ImagingStudy.modality or .started; the lean projector must
    still return a schema-valid item (`ref` is the only `required` field)."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "ImagingStudy", "id": "img-x"})

    client._get = fake_get  # type: ignore[assignment]
    priors = asyncio.run(client.search_imaging_studies("demo-1"))
    assert priors == [{"ref": "ImagingStudy/img-x"}]


# --- search_observations -----------------------------------------------------

def test_search_observations_joins_codes_with_comma():
    """FHIR search: `code=<a>,<b>` = OR. Joining the whole panel into one query
    turns N LOINC round-trips into 1 (matters when we ask for creatinine + every
    eGFR variant we care about for contrast decisions)."""
    client = Fhir2Client()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle({
            "resourceType": "Observation", "id": "creat-1",
            "code": {"coding": [{"system": "http://loinc.org", "code": "2160-0", "display": "Creatinine"}]},
            "valueQuantity": {"value": 1.1, "unit": "mg/dL"},
            "effectiveDateTime": "2026-06-20"})

    client._get = fake_get  # type: ignore[assignment]
    labs = asyncio.run(client.search_observations("demo-1", ["2160-0", "33914-3", "62238-1"]))
    assert calls[0] == ("Observation", {"patient": "demo-1", "code": "2160-0,33914-3,62238-1"})
    assert labs == [{"code": "2160-0", "display": "Creatinine",
                     "value": 1.1, "unit": "mg/dL", "date": "2026-06-20"}]


def test_search_observations_empty_codes_skips_the_call():
    client = Fhir2Client()
    called = False

    async def fake_get(path, params=None):
        nonlocal called
        called = True
        return _bundle()

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_observations("demo-1", [])) == []
    assert called is False


def test_search_observations_handles_valuestring():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "Observation", "id": "o1",
                        "code": {"coding": [{"code": "X"}]},
                        "valueString": "positive"})

    client._get = fake_get  # type: ignore[assignment]
    labs = asyncio.run(client.search_observations("demo-1", ["X"]))
    assert labs == [{"code": "X", "value": "positive"}]


# --- search_conditions -------------------------------------------------------

def test_search_conditions_asks_for_active_and_re_filters_clientside():
    """Sends `clinical-status=active` (the server SHOULD honor it) AND re-filters
    client-side (in case it doesn't — matches the poll_finalized_reports approach
    to OpenMRS's spotty search-param support)."""
    client = Fhir2Client()
    calls = []
    active = {"resourceType": "Condition", "id": "c1",
              "clinicalStatus": {"coding": [{"code": "active"}]},
              "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10",
                                   "code": "C34.1", "display": "Lung neoplasm"}]}}
    resolved = {"resourceType": "Condition", "id": "c2",
                "clinicalStatus": {"coding": [{"code": "resolved"}]},
                "code": {"coding": [{"code": "J18.9"}]}}

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(active, resolved)  # server returned both; we drop the resolved one

    client._get = fake_get  # type: ignore[assignment]
    problems = asyncio.run(client.search_conditions("demo-1"))
    assert calls[0] == ("Condition", {"patient": "demo-1", "clinical-status": "active"})
    assert problems == [{"code": "C34.1", "display": "Lung neoplasm"}]


def test_search_conditions_treats_missing_clinicalstatus_as_active():
    """Some deployments omit clinicalStatus entirely. Better to surface the problem
    than to hide it — the radiologist can judge relevance from the code."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "Condition", "id": "c1",
                        "code": {"coding": [{"code": "I10", "display": "HTN"}]}})

    client._get = fake_get  # type: ignore[assignment]
    problems = asyncio.run(client.search_conditions("demo-1"))
    assert problems == [{"code": "I10", "display": "HTN"}]


# --- search_allergies --------------------------------------------------------

def test_search_allergies_projects_code_and_criticality():
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "AllergyIntolerance", "id": "a1",
                        "code": {"coding": [{"code": "iodine-contrast"}]},
                        "criticality": "high"})

    client._get = fake_get  # type: ignore[assignment]
    allergies = asyncio.run(client.search_allergies("demo-1"))
    assert allergies == [{"code": "iodine-contrast", "criticality": "high"}]


def test_search_allergies_omits_missing_criticality():
    """Schema requires `code` only; `criticality` is optional — omit it when absent."""
    client = Fhir2Client()

    async def fake_get(path, params=None):
        return _bundle({"resourceType": "AllergyIntolerance", "id": "a2",
                        "code": {"coding": [{"code": "penicillin"}]}})

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_allergies("demo-1")) == [{"code": "penicillin"}]


# --- search_medications ------------------------------------------------------

def test_search_medications_returns_active_only_from_medicationCC():
    client = Fhir2Client()
    calls = []
    metformin = {"resourceType": "MedicationRequest", "id": "m1", "status": "active",
                 "medicationCodeableConcept": {
                     "coding": [{"system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                                 "code": "6809", "display": "Metformin"}]}}
    completed = {"resourceType": "MedicationRequest", "id": "m2", "status": "completed",
                 "medicationCodeableConcept": {"coding": [{"code": "11289"}]}}

    async def fake_get(path, params=None):
        calls.append((path, params))
        return _bundle(metformin, completed)

    client._get = fake_get  # type: ignore[assignment]
    meds = asyncio.run(client.search_medications("demo-1"))
    # No `status` param: live fhir2 500s on MedicationRequest?status=... so activeness is
    # filtered client-side. The completed med is dropped by _medication_is_active, not the server.
    assert calls[0] == ("MedicationRequest", {"patient": "demo-1"})
    assert meds == [{"code": "6809", "display": "Metformin"}]


def test_collect_treats_404_as_empty():
    """A resource type the fhir2 build doesn't expose (e.g. ImagingStudy on o3) answers 404;
    the search degrades to [] instead of raising, so the slice just goes empty."""
    import httpx

    client = Fhir2Client()

    async def fake_get(path, params=None):
        request = httpx.Request("GET", "http://fhir/ImagingStudy")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.search_imaging_studies("demo-1")) == []
