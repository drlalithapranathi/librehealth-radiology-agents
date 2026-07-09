"""Regression pins for OrthancStableStudyEvent (contracts/events/orthanc-stable.schema.json).

The Orthanc plugin has two implementations (Python primary, Lua fallback) and both
MUST emit an identical payload shape. The Lua path cannot be exercised in Python at
CI time — instead we pin a golden example of what the Lua produces and validate it
against the schema. The Python path CAN be exercised: its pure helpers are imported
here and their payload output is asserted schema-valid and structurally identical
to the Lua fixture. A schema change that breaks either contract is caught here.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from radagent_common.validation import validate_against, ContractError
from radagent_common import paths


SCHEMA = paths.contracts_dir() / "events" / "orthanc-stable.schema.json"

# The Python plugin lives under integrations/orthanc-plugin/ (outside pytest's normal
# test roots) so importing it requires a sys.path shim. The plugin wraps `import orthanc`
# in try/except so importing it here (no Orthanc runtime present) is a no-op — the
# module-level RegisterOnChangeCallback guard also skips, so this has no side effects.
_PLUGIN_DIR = paths.repo_root() / "integrations" / "orthanc-plugin"
sys.path.insert(0, str(_PLUGIN_DIR))
import orthanc_stable_study as py_plugin  # noqa: E402


# Field-for-field the payload BOTH plugins emit for _STUDY_RECORD below: modality
# read from RequestedTags, occurredAt reshaped from the DICOM-datetime LastUpdate
# ("20260707T123005") into RFC 3339. If either side changes, update the other AND
# this fixture in the same MR.
_LUA_EMITS = {
    "schemaVersion":    "1.0.0",
    "eventType":        "orthanc.study.stable",
    "orthancStudyId":   "aorta-study-001",
    "studyInstanceUID": "1.2.840.113619.2.55.3.111111111",
    "modality":         "CT",
    "accessionNumber":  "ACC-AORTA-001",
    "occurredAt":       "2026-07-07T12:30:05Z",
}


def test_lua_fallback_payload_matches_schema():
    """The Lua fallback's payload shape must validate against the event schema."""
    validate_against(_LUA_EMITS, SCHEMA)


def test_accession_number_is_optional():
    """Some scanners omit AccessionNumber; the schema must tolerate that so the Lua
    fallback (which passes '' or drops it entirely) does not have to synthesise one."""
    without_accession = {k: v for k, v in _LUA_EMITS.items() if k != "accessionNumber"}
    validate_against(without_accession, SCHEMA)


def test_missing_required_field_rejected():
    """Sanity: dropping a required field trips the ingress. This is the CI gate that
    catches an Orthanc build whose LastUpdate is missing (occurredAt) or whose
    Modality/ModalitiesInStudy are both absent."""
    for required in ("schemaVersion", "eventType", "orthancStudyId",
                     "studyInstanceUID", "modality", "occurredAt"):
        bad = {k: v for k, v in _LUA_EMITS.items() if k != required}
        with pytest.raises(ContractError):
            validate_against(bad, SCHEMA)


def test_eventtype_is_pinned_const():
    """The Lua and Python plugin BOTH hard-code 'orthanc.study.stable'; the schema
    enforces it as a const so a typo in either implementation is caught."""
    bad = dict(_LUA_EMITS, eventType="orthanc.study.STABLE")
    with pytest.raises(ContractError):
        validate_against(bad, SCHEMA)


# ---------------------------------------------------------------------------
# Python plugin (integrations/orthanc-plugin/orthanc_stable_study.py) — pure
# helpers unit-tested outside Orthanc.
# ---------------------------------------------------------------------------


# Representative /studies/{id}?requested-tags=ModalitiesInStudy REST view — the
# shape REAL Orthanc returns (verified against orthancteam/orthanc 1.12.x):
#   * LastUpdate is DICOM datetime 'YYYYMMDDTHHMMSS' (UTC), NOT RFC 3339.
#   * ModalitiesInStudy is a computed tag returned under RequestedTags, not MainDicomTags.
# build_event must reshape LastUpdate to RFC 3339 and read modality from RequestedTags;
# both paths then converge on _LUA_EMITS below.
_STUDY_RECORD = {
    "ID": "aorta-study-001",
    "MainDicomTags": {
        "StudyInstanceUID": "1.2.840.113619.2.55.3.111111111",
        "AccessionNumber":  "ACC-AORTA-001",
        "StudyDescription": "CT AORTA W CONTRAST",
    },
    "RequestedTags": {"ModalitiesInStudy": "CT"},
    "LastUpdate": "20260707T123005",
}


def test_python_plugin_payload_matches_schema():
    """The Python plugin's build_event output must validate against the shared schema."""
    payload = py_plugin.build_event("aorta-study-001", _STUDY_RECORD)
    validate_against(payload, SCHEMA)


def test_python_plugin_matches_lua_shape():
    """Byte-identical parity with the Lua fallback's payload. If a build change to
    either implementation makes their outputs diverge, the ingress will start seeing
    two different shapes depending on which plugin loaded — this test catches that."""
    payload = py_plugin.build_event("aorta-study-001", _STUDY_RECORD)
    assert payload == _LUA_EMITS


_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_python_plugin_converts_lastupdate_to_rfc3339():
    """Orthanc reports LastUpdate as DICOM datetime YYYYMMDDTHHMMSS (UTC). The plugin
    must reshape it to RFC 3339 so occurredAt satisfies the schema's format: date-time,
    rather than passing the DICOM shape straight through (which the live E2E caught)."""
    payload = py_plugin.build_event("s1", {**_STUDY_RECORD, "LastUpdate": "20200101T000000"})
    assert payload["occurredAt"] == "2020-01-01T00:00:00Z"
    assert _RFC3339.match(payload["occurredAt"])


def test_python_plugin_falls_through_when_lastupdate_missing():
    """Some Orthanc builds omit LastUpdate on the study record. The plugin must
    synthesise a valid RFC 3339 UTC timestamp so the event stays schema-valid."""
    record = {k: v for k, v in _STUDY_RECORD.items() if k != "LastUpdate"}
    payload = py_plugin.build_event("s1", record)
    validate_against(payload, SCHEMA)
    assert _RFC3339.match(payload["occurredAt"])


class TestToRfc3339Utc:
    def test_converts_dicom_datetime(self):
        assert py_plugin.to_rfc3339_utc("20260709T042319") == "2026-07-09T04:23:19Z"

    def test_passes_through_existing_rfc3339(self):
        assert py_plugin.to_rfc3339_utc("2026-07-07T12:30:05Z") == "2026-07-07T12:30:05Z"

    @pytest.mark.parametrize("bad", ["", "not-a-date", "2026", None, 20260709])
    def test_none_on_empty_or_unparseable(self, bad):
        assert py_plugin.to_rfc3339_utc(bad) is None


def test_python_plugin_modality_from_requested_tags():
    """Orthanc returns the computed ModalitiesInStudy under RequestedTags (absent from
    MainDicomTags by default), so the plugin must read it there — else modality is
    empty, as the live E2E showed. Mirrors the Lua fallback's lookup order."""
    record = {
        "MainDicomTags": {"StudyInstanceUID": "1.2.3", "AccessionNumber": "A1"},
        "RequestedTags": {"ModalitiesInStudy": "MG"},
        "LastUpdate": "20260707T123005",
    }
    payload = py_plugin.build_event("s1", record)
    assert payload["modality"] == "MG"


def test_python_plugin_missing_maindicomtags_stays_schema_valid():
    """Defensive: a study record with no MainDicomTags at all should still yield
    a schema-valid payload (empty strings), because the ingress guards on schema
    validation and we must not raise from the plugin (would crash the PACS)."""
    payload = py_plugin.build_event("s1", {"LastUpdate": "2026-07-07T12:30:05Z"})
    validate_against(payload, SCHEMA)
    assert payload["studyInstanceUID"] == ""
    assert payload["modality"] == ""


def test_python_plugin_requested_tags_fallback_for_accession():
    """Some Orthanc builds park AccessionNumber in RequestedTags rather than
    MainDicomTags. Both paths must find it — matches the Lua fallback's lookup order."""
    record = {
        "MainDicomTags": {"StudyInstanceUID": "1.2.3", "Modality": "CT"},
        "RequestedTags": {"AccessionNumber": "ACC-XYZ"},
        "LastUpdate": "2026-07-07T12:30:05Z",
    }
    payload = py_plugin.build_event("s1", record)
    assert payload["accessionNumber"] == "ACC-XYZ"


def test_python_plugin_none_record_stays_schema_valid():
    """The plugin's on_change swallows exceptions when RestApiGet fails, but the
    pure builder must ALSO tolerate None defensively so a future refactor that
    passes a missing record through does not silently emit malformed events."""
    payload = py_plugin.build_event("s1", None)
    validate_against(payload, SCHEMA)


class TestIsHttpUrl:
    """Mirror the Lua fallback's `isHttpUrl` guard (Bandit B310 / CWE-939): urlopen
    would happily dereference file:/ftp: schemes, so we reject non-http(s) up front."""

    @pytest.mark.parametrize("url", [
        "http://orchestrator:8090/webhooks/orthanc",
        "https://orch.example.com/webhooks/orthanc",
        "HTTP://localhost/webhook",  # scheme parsed case-insensitively
    ])
    def test_accepts_http_and_https(self, url):
        assert py_plugin.is_http_url(url)

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/loot",
        "gopher://example.com/",
        "",
        "not-a-url",
    ])
    def test_rejects_non_http(self, url):
        assert not py_plugin.is_http_url(url)
