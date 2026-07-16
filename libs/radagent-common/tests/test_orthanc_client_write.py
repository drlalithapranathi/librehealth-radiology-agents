"""Tests for the DICOM SC write path (#59).

Split from ``test_orthanc_client.py`` so that (a) the read-only tests stay hermetic and do not
pull the [imaging] extra unnecessarily, and (b) the write-path tests can enable
``ORTHANC_PRESIGN_WRITE_ENABLED`` via a fixture without touching the read tests' env.

Uses monkey-patched HTTP methods for the same reason as ``test_orthanc_client.py`` — no real
Orthanc, tight assertions on the payload we would have sent. The DICOM construction and the guards
are the interesting bits and both are proved in-process.
"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Optional

import pytest

from radagent_common.orthanc_client import (
    AI_EVIDENCE_SERIES_DESCRIPTION,
    EvidenceCaptureDisabled,
    InsecureWriteTransportError,
    OrthancClient,
    _build_evidence_capture_dcm,
    _deterministic_uid,
    _evidence_capture_enabled,
    _guard_write_transport,
    _is_plaintext_remote,
    _write_transport_is_secure,
)


# ---------------------------------------------------------------------------
# Sample target-instance tags: the shape Orthanc's /simplified-tags returns.
# Every test that builds an SC starts from a variation of this dict.
# ---------------------------------------------------------------------------
_TARGET_TAGS = {
    "SOPClassUID": "1.2.840.10008.5.1.4.1.1.2",   # CT Image Storage (arbitrary source)
    "StudyInstanceUID": "1.2.840.113619.2.55.3.111",
    "StudyDate": "20260707",
    "StudyTime": "120000",
    "AccessionNumber": "ACC-1",
    "PatientName": "DOE^JANE",
    "PatientID": "MRN-42",
    "PatientBirthDate": "19700101",
    "PatientSex": "F",
    "StudyDescription": "CXR CHEST 1 VIEW",
}

_TARGET_SOP_UID = "1.2.3.4.5.6"
_ORTHANC_STUDY_ID = "abc-123"
_TOOL_ID = "pneumothorax-detect"
_LABEL = "Pneumothorax"
_CONFIDENCE = 0.72


@pytest.fixture(autouse=False)
def _write_enabled(monkeypatch):
    """Flip the feature flag on for the tests that need the write to fire."""
    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")


@pytest.fixture(autouse=False)
def _write_disabled(monkeypatch):
    monkeypatch.delenv("ORTHANC_PRESIGN_WRITE_ENABLED", raising=False)


# ===========================================================================
# _build_evidence_capture_dcm: DICOM construction correctness
# ===========================================================================

def test_sc_has_all_the_authorship_stamps():
    """The build must set every discriminator we would use to guard an update-vs-create
    decision: SeriesDescription with our exact string, our UID root on both SOP and Series,
    Manufacturer, ModelName. Anything without these is not ours."""
    import pydicom

    dicom_bytes, new_sop_uid = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    assert ds.SeriesDescription.startswith(AI_EVIDENCE_SERIES_DESCRIPTION)
    assert ds.Manufacturer == "LibreHealth Radiology"
    assert ds.ManufacturerModelName == "lh-radiology-agents"
    assert new_sop_uid.startswith("2.25.")
    assert ds.SOPInstanceUID == new_sop_uid
    assert ds.SeriesInstanceUID.startswith("2.25.")
    # Different UIDs -- distinct namespaces for series and instance
    assert ds.SeriesInstanceUID != ds.SOPInstanceUID


def test_sc_copies_patient_and_study_identifiers_from_source():
    """PHI on write: copy exactly what the source has -- do not derive or invent. The SC
    only ever joins the same patient/study, which is what makes it appear in the reader's
    OHIF view of that study."""
    import pydicom

    dicom_bytes, _ = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    assert str(ds.PatientName) == "DOE^JANE"
    assert ds.PatientID == "MRN-42"
    assert ds.PatientBirthDate == "19700101"
    assert ds.PatientSex == "F"
    assert ds.StudyInstanceUID == _TARGET_TAGS["StudyInstanceUID"]
    assert ds.StudyDate == "20260707"
    assert ds.AccessionNumber == "ACC-1"
    assert ds.StudyDescription == "CXR CHEST 1 VIEW"


def test_sc_source_image_sequence_points_at_the_target():
    """OHIF surfaces SourceImageSequence when the radiologist inspects an SC series. The
    reference must point at the exact SOPInstanceUID the tool scored."""
    import pydicom

    dicom_bytes, _ = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    assert len(ds.SourceImageSequence) == 1
    ref = ds.SourceImageSequence[0]
    assert ref.ReferencedSOPInstanceUID == _TARGET_SOP_UID
    assert ref.ReferencedSOPClassUID == _TARGET_TAGS["SOPClassUID"]


def test_sc_series_description_carries_the_readable_label():
    """A radiologist scanning the OHIF study panel sees series names; the label plus
    confidence lands in SeriesDescription so the finding is legible without opening the pixels."""
    import pydicom

    dicom_bytes, _ = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label="Pneumothorax",
        confidence=0.72,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    assert "Pneumothorax" in ds.SeriesDescription
    assert "0.72" in ds.SeriesDescription


def test_sc_confidence_optional_series_description_still_readable():
    """Confidence is Optional in the API; a missing confidence must not produce a garbled
    'p=None' or a stray colon. Format degrades to just the label."""
    import pydicom

    dicom_bytes, _ = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label="Pneumothorax",
        confidence=None,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    assert "Pneumothorax" in ds.SeriesDescription
    assert "None" not in ds.SeriesDescription
    assert "p=" not in ds.SeriesDescription


def test_sc_is_a_valid_secondary_capture_class():
    """SOP Class UID must match the DICOM SC Image Storage class, not something else --
    OHIF and every other viewer route rendering off SOP Class."""
    import pydicom

    dicom_bytes, _ = _build_evidence_capture_dcm(
        target_tags=_TARGET_TAGS,
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    )
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes))
    # SC Image Storage.
    assert ds.SOPClassUID == "1.2.840.10008.5.1.4.1.1.7"
    assert ds.Modality == "OT"
    # Required SC image pixel tags: nonzero rows/cols, MONOCHROME2, 8-bit.
    assert ds.Rows > 0 and ds.Columns > 0
    assert ds.PhotometricInterpretation == "MONOCHROME2"
    assert ds.BitsAllocated == 8


# ===========================================================================
# Idempotency: (study, target, tool) → deterministic UIDs
# ===========================================================================

def test_deterministic_uids_same_inputs_same_uids():
    a = _deterministic_uid("sop", "study-a", "target-1", "pneumothorax-detect")
    b = _deterministic_uid("sop", "study-a", "target-1", "pneumothorax-detect")
    assert a == b


def test_deterministic_uids_different_tool_different_uid():
    """Two tools scoring the same target must produce distinct evidence captures.
    Otherwise the second write silently overwrites the first (Orthanc de-duplicates on
    SOPInstanceUID collision)."""
    a = _deterministic_uid("sop", "study-a", "target-1", "pneumothorax-detect")
    b = _deterministic_uid("sop", "study-a", "target-1", "pe-detect")
    assert a != b


def test_deterministic_uids_different_target_different_uid():
    """One tool scoring two instances in the same study must produce distinct captures."""
    a = _deterministic_uid("sop", "study-a", "target-1", "pneumothorax-detect")
    b = _deterministic_uid("sop", "study-a", "target-2", "pneumothorax-detect")
    assert a != b


def test_deterministic_uid_separator_avoids_field_collision():
    """('a', 'bc') must not hash to the same UID as ('ab', 'c'). The NUL separator prevents that."""
    a = _deterministic_uid("a", "bc")
    b = _deterministic_uid("ab", "c")
    assert a != b


# ===========================================================================
# Transport guard (#30 pattern, mirrored for Orthanc)
# ===========================================================================

def test_https_is_never_plaintext():
    assert not _is_plaintext_remote("https://orthanc.example.com:8042")


def test_http_loopback_is_not_plaintext_remote():
    for host in ("localhost", "127.0.0.1"):
        assert not _is_plaintext_remote(f"http://{host}:8042")


def test_http_to_remote_host_is_plaintext_remote():
    assert _is_plaintext_remote("http://orthanc.example.com:8042")


def test_write_transport_secure_when_loopback_or_https(monkeypatch):
    monkeypatch.delenv("ORTHANC_ALLOW_INSECURE_WRITE", raising=False)
    assert _write_transport_is_secure("https://orthanc.example.com:8042")
    assert _write_transport_is_secure("http://localhost:8042")
    assert not _write_transport_is_secure("http://orthanc.example.com:8042")


def test_write_transport_secure_with_opt_in(monkeypatch):
    """A trusted-internal-network deployment can opt in via ORTHANC_ALLOW_INSECURE_WRITE. Mirrors
    fhir2's FHIR2_ALLOW_INSECURE_WRITE opt-in from #30."""
    monkeypatch.setenv("ORTHANC_ALLOW_INSECURE_WRITE", "1")
    assert _write_transport_is_secure("http://orthanc.example.com:8042")


def test_guard_raises_for_plaintext_to_remote(monkeypatch):
    monkeypatch.delenv("ORTHANC_ALLOW_INSECURE_WRITE", raising=False)
    with pytest.raises(InsecureWriteTransportError):
        _guard_write_transport("http://orthanc.example.com:8042")


def test_guard_silent_for_https_or_loopback(monkeypatch):
    monkeypatch.delenv("ORTHANC_ALLOW_INSECURE_WRITE", raising=False)
    _guard_write_transport("https://orthanc.example.com:8042")  # must not raise
    _guard_write_transport("http://localhost:8042")


# ===========================================================================
# Feature gate: ORTHANC_PRESIGN_WRITE_ENABLED
# ===========================================================================

def test_write_no_ops_when_feature_gate_off(monkeypatch, caplog):
    """The feature gate is deployment-level: the write must be a documented no-op that returns
    None and logs, NOT a raise, when disabled. Callers can call it and get None back without
    trying to reason about whether it is safe."""
    monkeypatch.delenv("ORTHANC_PRESIGN_WRITE_ENABLED", raising=False)
    client = OrthancClient(base_url="http://orthanc:8042")
    calls: list[str] = []

    async def fake_find(*a, **kw):
        calls.append("find")
        return "should-not-be-called"

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]

    result = asyncio.run(client.write_ai_evidence_capture(
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    ))
    assert result is None
    assert calls == []  # gate short-circuits BEFORE any Orthanc IO


def test_feature_gate_env_var_accepts_common_truthy_values(monkeypatch):
    """Standard docker-compose truthy values: '1', 'true', 'TRUE', 'yes'. Anything else is off.
    This matches the fhir2 pattern; mismatched truthy semantics between guards would surprise
    an operator flipping one flag and not the other."""
    for on in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", on)
        assert _evidence_capture_enabled(), f"{on!r} should enable the write"
    for off in ("", "0", "false", "FALSE", "no", "definitely"):
        monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", off)
        assert not _evidence_capture_enabled(), f"{off!r} should NOT enable the write"


# ===========================================================================
# write_ai_evidence_capture: full flow with fakes
# ===========================================================================

def test_write_success_returns_new_sop_uid(monkeypatch):
    """Happy path: gate on, target resolves, tags fetched, SC built, POSTed to Orthanc. Return
    is the new instance's SOPInstanceUID."""
    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")
    client = OrthancClient(base_url="http://localhost:8042")
    posted: list[bytes] = []

    async def fake_find(sop_uid):
        assert sop_uid == _TARGET_SOP_UID
        return "orthanc-instance-id-99"

    async def fake_tags(orthanc_id):
        assert orthanc_id == "orthanc-instance-id-99"
        return dict(_TARGET_TAGS)

    async def fake_post_instance(payload):
        posted.append(payload)
        return {"ID": "new-orthanc-id", "Status": "Success"}

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]
    client.get_instance_tags = fake_tags  # type: ignore[assignment]
    client._post_instance = fake_post_instance  # type: ignore[assignment]

    result = asyncio.run(client.write_ai_evidence_capture(
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    ))
    assert result is not None
    assert result.startswith("2.25.")
    assert len(posted) == 1
    # Confirm the bytes look like a DICOM file (preamble + DICM magic).
    assert posted[0][128:132] == b"DICM"


def test_write_returns_none_when_target_not_in_orthanc(monkeypatch):
    """If the classifier's target SOPInstanceUID isn't in Orthanc (evidence points at a
    non-existent instance), skip cleanly. Best-effort: log + None, never raise."""
    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")
    client = OrthancClient(base_url="http://localhost:8042")

    async def fake_find(sop_uid):
        return None

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]

    result = asyncio.run(client.write_ai_evidence_capture(
        target_sop_instance_uid="does-not-exist",
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    ))
    assert result is None


def test_write_returns_none_on_orthanc_http_error(monkeypatch):
    """Orthanc down or 500: swallow + log + None. The human read is the safety net."""
    import httpx

    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")
    client = OrthancClient(base_url="http://localhost:8042")

    async def fake_find(sop_uid):
        return "orthanc-instance-id-99"

    async def fake_tags(orthanc_id):
        return dict(_TARGET_TAGS)

    async def fake_post_instance(payload):
        req = httpx.Request("POST", "http://localhost:8042/instances")
        raise httpx.HTTPStatusError(
            "orthanc 500", request=req, response=httpx.Response(500, request=req),
        )

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]
    client.get_instance_tags = fake_tags  # type: ignore[assignment]
    client._post_instance = fake_post_instance  # type: ignore[assignment]

    result = asyncio.run(client.write_ai_evidence_capture(
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    ))
    assert result is None


def test_write_transport_refusal_re_raises_not_swallowed(monkeypatch):
    """Transport refusal is a deployment policy signal, not an outage. It must reach the caller
    -- swallowing it would let a misconfigured deployment silently skip every SC forever, which
    is worse than the failure it protects against."""
    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")
    monkeypatch.delenv("ORTHANC_ALLOW_INSECURE_WRITE", raising=False)
    client = OrthancClient(base_url="http://orthanc.example.com:8042")  # plaintext + remote

    async def fake_find(sop_uid):
        return "orthanc-instance-id-99"

    async def fake_tags(orthanc_id):
        return dict(_TARGET_TAGS)

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]
    client.get_instance_tags = fake_tags  # type: ignore[assignment]

    with pytest.raises(InsecureWriteTransportError):
        asyncio.run(client.write_ai_evidence_capture(
            target_sop_instance_uid=_TARGET_SOP_UID,
            orthanc_study_id=_ORTHANC_STUDY_ID,
            tool_id=_TOOL_ID,
            label=_LABEL,
            confidence=_CONFIDENCE,
        ))


def test_write_returns_none_on_import_error(monkeypatch):
    """Deployment installed radagent-common without the [imaging] extra. The import inside
    _build_evidence_capture_dcm raises ImportError with an actionable message, which the write
    catches (best-effort contract). Documented consequence: install the extra, or the write
    silently no-ops."""
    monkeypatch.setenv("ORTHANC_PRESIGN_WRITE_ENABLED", "1")
    client = OrthancClient(base_url="http://localhost:8042")

    async def fake_find(sop_uid):
        return "orthanc-instance-id-99"

    async def fake_tags(orthanc_id):
        return dict(_TARGET_TAGS)

    def fake_build(*a, **kw):
        raise ImportError(
            "AI evidence-capture write requires the [imaging] extra: install radagent-common with "
            "the imaging extras (pydicom, numpy) enabled. See docs/dicom-evidence-writeback.md."
        )

    from radagent_common import orthanc_client as oc
    monkeypatch.setattr(oc, "_build_evidence_capture_dcm", fake_build)

    client.find_instance_by_sop_uid = fake_find  # type: ignore[assignment]
    client.get_instance_tags = fake_tags  # type: ignore[assignment]

    result = asyncio.run(client.write_ai_evidence_capture(
        target_sop_instance_uid=_TARGET_SOP_UID,
        orthanc_study_id=_ORTHANC_STUDY_ID,
        tool_id=_TOOL_ID,
        label=_LABEL,
        confidence=_CONFIDENCE,
    ))
    assert result is None


# ===========================================================================
# Auth: half-set creds fail loudly (same pattern as fhir2)
# ===========================================================================

def test_half_set_orthanc_creds_fail_loudly(monkeypatch):
    """One of user/pass without the other is a real deployment misconfiguration -- e.g., an
    ops person adding ORTHANC_BASIC_USER and forgetting the password. Prefer a loud RuntimeError
    to silent unauthenticated calls that fail elsewhere."""
    monkeypatch.setenv("ORTHANC_BASIC_USER", "alice")
    monkeypatch.delenv("ORTHANC_BASIC_PASS", raising=False)
    with pytest.raises(RuntimeError, match="together or not at all"):
        OrthancClient()
    monkeypatch.delenv("ORTHANC_BASIC_USER", raising=False)
    monkeypatch.setenv("ORTHANC_BASIC_PASS", "s3cret")
    with pytest.raises(RuntimeError, match="together or not at all"):
        OrthancClient()


def test_no_auth_env_stays_unauthenticated(monkeypatch):
    monkeypatch.delenv("ORTHANC_BASIC_USER", raising=False)
    monkeypatch.delenv("ORTHANC_BASIC_PASS", raising=False)
    client = OrthancClient()
    assert client._auth is None


def test_both_auth_env_present_is_used(monkeypatch):
    monkeypatch.setenv("ORTHANC_BASIC_USER", "alice")
    monkeypatch.setenv("ORTHANC_BASIC_PASS", "s3cret")
    client = OrthancClient()
    assert client._auth == ("alice", "s3cret")
