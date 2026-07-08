"""Regression pins for OrthancStableStudyEvent (contracts/events/orthanc-stable.schema.json).

The Orthanc plugin has two implementations (Python primary, Lua fallback) and both
MUST emit an identical payload shape. There is no Python that exercises the Lua at
CI time — instead we pin a golden example of what the Lua produces and validate it
against the schema, so a schema change that breaks the Lua contract is caught here.
"""
from __future__ import annotations

import pytest

from radagent_common.validation import validate_against, ContractError
from radagent_common import paths


SCHEMA = paths.contracts_dir() / "events" / "orthanc-stable.schema.json"


# Field-for-field the payload built by integrations/orthanc-plugin/orthanc_stable_study.lua.
# If either side changes, update the other AND this fixture in the same MR.
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
