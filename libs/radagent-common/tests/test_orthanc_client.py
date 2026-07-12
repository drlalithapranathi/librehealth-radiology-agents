"""Tests for OrthancClient (issue #20).

Uses monkey-patched `_get` to intercept the HTTP call — same pattern as
test_fhir_client. Focus is on:
  * URL / params construction (single round trip via ?expand=1)
  * lean-projection shape (never raises on partial records)
  * schema-compat guarantees for the join in the Worklist API
"""
from __future__ import annotations

import asyncio

from radagent_common.orthanc_client import OrthancClient, _lean_study


def test_lean_study_projects_all_common_fields():
    raw = {
        "ID": "aorta-study-001",
        "MainDicomTags": {
            "StudyInstanceUID": "1.2.840.113619.2.55.3.111111111",
            "AccessionNumber":  "ACC-AORTA-001",
            "ModalitiesInStudy": "CT",
            "StudyDescription": "CT AORTA W CONTRAST",
            "StudyDate":        "20260707",
        },
        "LastUpdate": "20260707T123005",
    }
    out = _lean_study(raw)
    assert out == {
        "orthancStudyId":    "aorta-study-001",
        "studyInstanceUID":  "1.2.840.113619.2.55.3.111111111",
        "accessionNumber":   "ACC-AORTA-001",
        "modality":          "CT",
        "studyDescription":  "CT AORTA W CONTRAST",
        "studyDate":         "20260707",
        "lastUpdate":        "20260707T123005",
    }


def test_lean_study_prefers_modalitiesinstudy_but_falls_back_to_modality():
    """Some builds only populate the single-modality Modality tag; the lean
    projector must find it either way."""
    raw = {"ID": "x", "MainDicomTags": {"Modality": "MR"}}
    assert _lean_study(raw)["modality"] == "MR"


def test_lean_study_tolerates_missing_maindicomtags():
    """Defensive: a partial Orthanc record must not raise (would knock a
    study off the worklist entirely). Every string field degrades to ''."""
    out = _lean_study({"ID": "x"})
    assert out["orthancStudyId"] == "x"
    assert out["studyInstanceUID"] == ""
    assert out["modality"] == ""


def test_lean_study_omits_instance_count():
    """numberOfInstances is not projected: /studies?expand carries no Statistics
    block, so the field would always be null. It must be absent, not null."""
    out = _lean_study({"ID": "x", "MainDicomTags": {"StudyInstanceUID": "1.2.3"},
                       "Statistics": {"CountInstances": 280}})
    assert "numberOfInstances" not in out


def test_list_completed_studies_uses_expand_single_round_trip():
    client = OrthancClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append((path, params))
        return [{"ID": "s1", "MainDicomTags": {"StudyInstanceUID": "1.2.3"}},
                {"ID": "s2", "MainDicomTags": {"StudyInstanceUID": "1.2.4"}}]

    client._get = fake_get  # type: ignore[assignment]
    studies = asyncio.run(client.list_completed_studies())
    # Single round trip via ?expand=1 — critical for the join in the Worklist API
    # to stay O(1) rather than N+1 as the worklist grows.
    assert calls == [("studies", {"expand": True})]
    assert [s["orthancStudyId"] for s in studies] == ["s1", "s2"]


def test_list_completed_studies_empty():
    """Orthanc with no studies must return [] (not None) — the caller uses
    this in a for-loop directly."""
    client = OrthancClient()

    async def fake_get(path, params=None):
        return []

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.list_completed_studies()) == []


def test_list_completed_studies_none_response_still_returns_list():
    """A malformed Orthanc reply (None instead of []) still yields [] rather
    than a TypeError in the caller."""
    client = OrthancClient()

    async def fake_get(path, params=None):
        return None

    client._get = fake_get  # type: ignore[assignment]
    assert asyncio.run(client.list_completed_studies()) == []


def test_get_study_fetches_by_orthanc_id():
    client = OrthancClient()
    calls = []

    async def fake_get(path, params=None):
        calls.append(path)
        return {"ID": "abc", "MainDicomTags": {}}

    client._get = fake_get  # type: ignore[assignment]
    got = asyncio.run(client.get_study("abc"))
    assert calls == ["studies/abc"]
    assert got["ID"] == "abc"
