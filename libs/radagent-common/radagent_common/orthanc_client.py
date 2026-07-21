"""Thin Orthanc REST client. Reads imaging metadata (lean-reference) and, since #59, writes AI
evidence back as DICOM Secondary Capture objects so CAD marks render in the viewer.

The write path is a real safety change, not a helper: Orthanc was read-only until this MR (golden
rule 2: "Orthanc is a fetch path"), and the SC object it creates carries pixel data and patient
identifiers -- PHI on write. Guards mirror the #26/#55/#68/#30 fhir2 write pattern:
  * gated on a COMPLETE finding with a resolvable target SOPInstanceUID (caller's job);
  * held behind ORTHANC_PRESIGN_WRITE_ENABLED, default False, so the write is inert until
    PI/lead sign-off flips it -- same shape as fhir2 held behind #30;
  * transport-refused over plaintext HTTP to a non-loopback host (unless the deployment opts
    into ORTHANC_ALLOW_INSECURE_WRITE for a trusted internal network);
  * authorship-stamped by SeriesDescription and by our own UID root -- an update never touches
    an object the AI did not author;
  * idempotent: same (orthanc_study_id, target_sop_instance_uid, tool_id) tuple deterministically
    produces the same new SOPInstanceUID, so Orthanc de-duplicates on re-run;
  * best-effort: failure returns None and logs a warning, never raises -- the human read is
    the safety net, and a failed evidence-capture write must never strand it.
"""
from __future__ import annotations
from typing import Any, Optional, Union
from urllib.parse import urlparse
import logging
import os
import uuid

import httpx


# --- Authorship + UID roots -------------------------------------------------
# We own our UID namespace under the DICOM UUID-derived scheme (PS3.5 B.2): any UUID can be
# expressed as a UID under the 2.25 root by taking the UUID's 128-bit integer form. Deterministic
# UUID5 hashing from a documented seed ties the UID back to a reproducible input -- same pattern
# as the concept UUIDs in #55. See docs/dicom-evidence-writeback.md for the seed derivation.
_UUID_UID_ROOT = "2.25"
_AUTHORSHIP_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "librehealth.org")
_AUTHORSHIP_SEED = "lh-radiology.ai-evidence-capture.v1"

# The SeriesDescription that stamps every AI evidence capture. Anything without this string in
# `SeriesDescription` is not ours -- the guard against overwriting a radiologist's or the modality's
# own SC. Deliberately human-readable so a radiologist opening the series in OHIF knows what they
# are looking at.
AI_EVIDENCE_SERIES_DESCRIPTION = "LH Radiology AI pre-sign impression draft"

# Manufacturer/model tags: authorship-visible in every DICOM tag browser.
_MANUFACTURER = "LibreHealth Radiology"
_MANUFACTURER_MODEL_NAME = "lh-radiology-agents"


class InsecureWriteTransportError(RuntimeError):
    """Refused an Orthanc write over plaintext HTTP to a non-loopback host.

    Same shape and rationale as `fhir_client.InsecureWriteTransportError` (#30): a DICOM SC write
    carries PHI (patient identifiers copied from the source study, plus the AI label) and rides
    HTTP Basic credentials in every request. Over plaintext to a remote host, both go on the wire
    in cleartext. This is a hard refusal unless the deployment sets ORTHANC_ALLOW_INSECURE_WRITE=1
    on a trusted internal network.
    """


class EvidenceCaptureDisabled(RuntimeError):
    """Held behind ORTHANC_PRESIGN_WRITE_ENABLED. The env var defaults False, so the write path is
    inert on first boot even for callers that would otherwise fire it. Flipped to True only after
    the PI/lead sign-off documented in docs/dicom-evidence-writeback.md."""


_log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_plaintext_remote(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.scheme != "https" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS


def _write_transport_is_secure(base_url: str) -> bool:
    """Is it safe to send an Orthanc WRITE to this base URL? Mirrors fhir2 (#30)."""
    if not _is_plaintext_remote(base_url):
        return True
    return os.environ.get("ORTHANC_ALLOW_INSECURE_WRITE", "").strip() in {"1", "true", "TRUE", "yes"}


def _guard_write_transport(base_url: str) -> None:
    if not _write_transport_is_secure(base_url):
        raise InsecureWriteTransportError(
            "refusing an Orthanc write over plaintext HTTP to a non-loopback host: the DICOM SC "
            "carries PHI (patient identifiers copied from the source study) and the HTTP Basic "
            "credentials would travel in cleartext. Use an https base URL, or set "
            "ORTHANC_ALLOW_INSECURE_WRITE=1 for a trusted internal network."
        )
    if _is_plaintext_remote(base_url):
        _log.warning(
            "Orthanc write proceeding over PLAINTEXT http to %s under ORTHANC_ALLOW_INSECURE_WRITE: "
            "the DICOM SC (PHI) and HTTP Basic credentials are in cleartext on this hop",
            urlparse(base_url).hostname,
        )


def _evidence_capture_enabled() -> bool:
    """Feature gate for the write. False by default so a fresh deployment cannot write pre-read
    AI objects into the archive without an explicit flip -- the deployment-level equivalent of the
    #26 COMPLETE gate on the fhir2 write path.

    Kept as an env var rather than a config file so a deployment can flip it without a rebuild,
    and so a test can flip it in-process without patching config-loading code paths.
    """
    return os.environ.get("ORTHANC_PRESIGN_WRITE_ENABLED", "").strip() in {"1", "true", "TRUE", "yes"}


def _deterministic_uid(*parts: str) -> str:
    """A stable DICOM UID derived from a namespaced UUID5 hash of the input parts.

    The 2.25 root means "the number after the dot is the decimal representation of a UUID"
    (DICOM PS3.5 B.2), so any UUID trivially becomes a valid UID with no registration. Two calls
    with the same parts produce byte-identical UIDs, which is how we make the write idempotent:
    a re-run derives the same target SOPInstanceUID and Orthanc de-duplicates on ingest.
    """
    name = "\x00".join(parts)  # NUL separator: two `("a", "bc")` never collides with `("ab", "c")`
    uid = uuid.uuid5(_AUTHORSHIP_NAMESPACE, f"{_AUTHORSHIP_SEED}::{name}")
    return f"{_UUID_UID_ROOT}.{uid.int}"


# ----------------------------------------------------------------------------
# The client
# ----------------------------------------------------------------------------

class OrthancClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        auth: Optional[tuple[str, str]] = None,
    ):
        self.base_url = (base_url or os.environ.get("ORTHANC_BASE_URL", "http://orthanc:8042")).rstrip("/")
        self._timeout = timeout
        # Optional HTTP Basic. Mirrors fhir2's pattern: pull creds from env, refuse a half-set
        # pair. `auth=None` means "no credentials," which is the dev-stack default (Orthanc's
        # anonymous mode). A production deployment sets both.
        if auth is None:
            u = os.environ.get("ORTHANC_BASIC_USER") or None
            p = os.environ.get("ORTHANC_BASIC_PASS") or None
            if (u is None) != (p is None):
                raise RuntimeError(
                    "ORTHANC_BASIC_USER and ORTHANC_BASIC_PASS must be set together or not at all"
                )
            auth = (u, p) if u is not None else None
        self._auth = auth

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
            r.raise_for_status()
            return r.json()

    async def _post_instance(self, dicom_bytes: bytes) -> dict:
        """POST /instances -- the Orthanc endpoint for uploading a raw DICOM file.

        Guards the transport up front: an Orthanc write carries PHI and the HTTP Basic
        credentials, so plaintext to a remote host is refused. Returns Orthanc's ingest
        response as parsed JSON (fields: `ID`, `ParentPatient`, `ParentStudy`, `ParentSeries`,
        `Path`, `Status`).
        """
        _guard_write_transport(self.base_url)  # no PHI/credentials over plaintext to a remote host (#59)
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.post(
                f"{self.base_url}/instances",
                content=dicom_bytes,
                headers={"Content-Type": "application/dicom"},
            )
            r.raise_for_status()
            return r.json()

    # --- read path (unchanged) ----------------------------------------------

    async def get_study(self, orthanc_study_id: str) -> dict:
        return await self._get(f"studies/{orthanc_study_id}")

    async def get_study_description(self, orthanc_study_id: str) -> str:
        """The study's DICOM StudyDescription, or "" when Orthanc has no such tag (#62)."""
        raw = await self.get_study(orthanc_study_id)
        main_tags = (raw or {}).get("MainDicomTags") or {}
        return (main_tags.get("StudyDescription") or "").strip()

    async def list_completed_studies(self) -> list[dict]:
        """Reading worklist source (#20). Every study Orthanc knows about, projected lean."""
        raw = await self._get("studies", params={"expand": True})
        return [_lean_study(s) for s in raw or []]

    async def get_instance_tags(self, orthanc_instance_id: str) -> dict:
        """Simplified DICOM tags for an instance -- the identifiers we need to copy into an
        SC that references it. Orthanc returns tag NAMES (not hex codes) at `?simplify`."""
        return await self._get(f"instances/{orthanc_instance_id}/simplified-tags")

    async def find_instance_by_sop_uid(self, sop_instance_uid: str) -> Optional[str]:
        """Orthanc's instance ID for a given DICOM SOPInstanceUID, or None if unknown.

        The SC write needs to fetch the target instance's tags to copy patient/study
        identifiers, and the caller only has the DICOM UID -- not Orthanc's internal ID.
        This maps one to the other via /tools/find.
        """
        try:
            hits = await self._post_find({"Level": "Instance", "Query": {"SOPInstanceUID": sop_instance_uid}})
        except httpx.HTTPStatusError:
            return None
        return hits[0] if hits else None

    async def _post_find(self, query: dict) -> list[str]:
        """Read-side POST to Orthanc's /tools/find endpoint. Not a PHI write -- carries a UID
        looking to be resolved -- but it does use POST as an HTTP verb. Skips the write guard
        because the payload is not PHI: it is the UID the caller already has."""
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.post(f"{self.base_url}/tools/find", json=query)
            r.raise_for_status()
            return r.json()

    # --- write path (#59) ---------------------------------------------------

    async def write_ai_evidence_capture(
        self,
        target_sop_instance_uid: str,
        orthanc_study_id: str,
        tool_id: str,
        label: str,
        confidence: Optional[float] = None,
    ) -> Optional[str]:
        """Create a DICOM Secondary Capture that represents an AI evidence marker for
        ``target_sop_instance_uid`` and store it in Orthanc, in a new authorship-stamped series
        under the same study. Returns the new SC's SOPInstanceUID on success, or None on any
        best-effort failure. This is the write path #59 opens.

        Held behind three gates:
          * ``ORTHANC_PRESIGN_WRITE_ENABLED`` deployment feature flag -- default False;
          * transport guard against plaintext-to-remote (see :func:`_guard_write_transport`);
          * authorship-stamped ``SeriesDescription`` + our UID root, so an idempotent re-run
            never touches an object we did not author.

        Idempotent per ``(orthanc_study_id, target_sop_instance_uid, tool_id)``: the new
        SC's SOPInstanceUID is derived deterministically from that tuple, so Orthanc's ingest
        will collapse re-runs onto the same instance (Orthanc keys instances by SOPInstanceUID).

        Best-effort: any failure -- disabled by the flag, target instance not found in Orthanc,
        DICOM build error, Orthanc HTTP failure -- returns None and logs. Never raises.
        """
        if not _evidence_capture_enabled():
            _log.info(
                "Orthanc evidence-capture write is disabled (ORTHANC_PRESIGN_WRITE_ENABLED unset); "
                "no SC written for %s tool=%s",
                target_sop_instance_uid, tool_id,
            )
            return None

        try:
            # Resolve the target so we can copy the identifiers that make the SC join the study.
            orthanc_instance_id = await self.find_instance_by_sop_uid(target_sop_instance_uid)
            if orthanc_instance_id is None:
                _log.warning(
                    "SC write skipped: target SOPInstanceUID %s not found in Orthanc "
                    "(study=%s tool=%s)",
                    target_sop_instance_uid, orthanc_study_id, tool_id,
                )
                return None
            target_tags = await self.get_instance_tags(orthanc_instance_id)

            dicom_bytes, new_sop_uid = _build_evidence_capture_dcm(
                target_tags=target_tags,
                target_sop_instance_uid=target_sop_instance_uid,
                orthanc_study_id=orthanc_study_id,
                tool_id=tool_id,
                label=label,
                confidence=confidence,
            )
            await self._post_instance(dicom_bytes)
            _log.info(
                "wrote AI evidence capture: sop=%s series-desc=%r tool=%s target-sop=%s",
                new_sop_uid, AI_EVIDENCE_SERIES_DESCRIPTION, tool_id, target_sop_instance_uid,
            )
            return new_sop_uid
        except InsecureWriteTransportError:
            # A transport refusal is a policy signal, not an outage: re-raise so the caller
            # can see the deployment misconfiguration rather than silently swallow it.
            raise
        except Exception as e:  # noqa: BLE001 -- best-effort by contract
            _log.warning(
                "AI evidence-capture write failed for target %s tool=%s: %s. Continuing without "
                "the SC; the pre-sign impression text alone carries the finding.",
                target_sop_instance_uid, tool_id, e,
            )
            return None


# ----------------------------------------------------------------------------
# DICOM SC construction
# ----------------------------------------------------------------------------

def _build_evidence_capture_dcm(
    target_tags: dict,
    target_sop_instance_uid: str,
    orthanc_study_id: str,
    tool_id: str,
    label: str,
    confidence: Optional[float],
) -> tuple[bytes, str]:
    """Assemble a valid Secondary Capture Image Storage instance that references
    ``target_sop_instance_uid`` via its SourceImageSequence and lives in a new authorship-stamped
    series under the same StudyInstanceUID as the target.

    Returns ``(dicom_bytes, new_sop_instance_uid)``. Deterministic: same inputs → same SOPInstanceUID.

    Pixel data is intentionally minimal (a small monochrome gradient). The clinical signal rides in
    the tags: ``SeriesDescription`` carries the human label ("LH Radiology AI pre-sign impression
    draft: Pneumothorax p=0.72"), ``ImageComments`` carries the same for a tag browser, and
    ``SourceImageSequence`` points back at the scored instance. Rendering a burned-in text overlay
    would be visually louder but adds Pillow as a dependency and picks fonts + layout for a
    feature that's inert until #30 signs off -- not this MR's job. Follow-up when the write turns on.
    """
    # Lazy imports so an installation without the [imaging] extra can still import this module
    # for the read-only paths. If a caller reaches the write path without the extras, they get a
    # clear ImportError with actionable text.
    try:
        import pydicom
        from pydicom.dataset import Dataset, FileMetaDataset
        from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
        import numpy as np
    except ImportError as e:  # noqa: F841 -- we re-raise a friendlier one
        raise ImportError(
            "AI evidence-capture write requires the [imaging] extra: install radagent-common with "
            "the imaging extras (pydicom, numpy) enabled. See docs/dicom-evidence-writeback.md."
        ) from None

    # Deterministic UIDs so the write is idempotent per (study, target, tool).
    new_sop_instance_uid = _deterministic_uid(
        "sop", orthanc_study_id, target_sop_instance_uid, tool_id,
    )
    new_series_instance_uid = _deterministic_uid(
        "series", orthanc_study_id, tool_id,
    )

    # ---- File Meta ---------------------------------------------------------
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = new_sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = Dataset()
    ds.file_meta = file_meta

    # ---- SOP Common --------------------------------------------------------
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = new_sop_instance_uid

    # ---- Patient (copied from the target instance) -------------------------
    # PHI: identifiers copied from the source study so the SC joins the same patient. Copy
    # exactly what the source has; do not derive or invent.
    for tag in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex"):
        value = target_tags.get(tag)
        if value:
            setattr(ds, tag, value)

    # ---- Study (copied from target, so this SC lands in the same study) ----
    ds.StudyInstanceUID = target_tags.get("StudyInstanceUID", "")
    ds.StudyDate = target_tags.get("StudyDate", "")
    ds.StudyTime = target_tags.get("StudyTime", "")
    ds.AccessionNumber = target_tags.get("AccessionNumber", "")
    ds.ReferringPhysicianName = target_tags.get("ReferringPhysicianName", "")
    ds.StudyID = target_tags.get("StudyID", "")
    ds.StudyDescription = target_tags.get("StudyDescription", "")

    # ---- Series (new -- our authorship stamp) ------------------------------
    ds.SeriesInstanceUID = new_series_instance_uid
    ds.SeriesNumber = "9001"  # High so it sorts after the source imaging series in most viewers
    ds.Modality = "OT"        # Other, per SC convention for AI-generated evidence
    ds.SeriesDescription = _series_description_with_label(label, confidence)

    # ---- Equipment (authorship-visible in every tag browser) ---------------
    ds.Manufacturer = _MANUFACTURER
    ds.ManufacturerModelName = _MANUFACTURER_MODEL_NAME
    ds.SoftwareVersions = "lh-radiology-agents #59"

    # ---- Source image reference (the target the tool scored) ---------------
    # ``SourceImageSequence`` is how a Secondary Capture points at the primary image it was
    # derived from -- OHIF surfaces this when the radiologist inspects the SC series.
    source_ref = Dataset()
    source_ref.ReferencedSOPClassUID = target_tags.get("SOPClassUID", "")
    source_ref.ReferencedSOPInstanceUID = target_sop_instance_uid
    ds.SourceImageSequence = [source_ref]

    # ---- Image comments carry the same label for a tag browser -------------
    ds.ImageComments = _image_comment_with_label(label, tool_id, confidence)

    # ---- General Image + SC image module (required tags for a valid SC) ----
    ds.InstanceNumber = "1"
    ds.PatientOrientation = ""
    ds.ContentDate = ds.StudyDate
    ds.ContentTime = ds.StudyTime
    ds.ConversionType = "WSD"  # Workstation - Secondary capture derived at a workstation
    # A minimal monochrome gradient: 32x32, 8-bit unsigned. Deliberately tiny -- this is a marker,
    # not diagnostic content. Rendering an actual label overlay is a follow-up (see docstring).
    pixel_array = np.tile(np.arange(32, dtype=np.uint8), (32, 1))
    ds.Rows = pixel_array.shape[0]
    ds.Columns = pixel_array.shape[1]
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = pixel_array.tobytes()

    # Serialize.
    buf = _dcm_to_bytes(ds)
    return buf, new_sop_instance_uid


def _series_description_with_label(label: str, confidence: Optional[float]) -> str:
    """Format the SeriesDescription so a radiologist scanning the OHIF study panel sees the
    authorship stamp AND the finding, without having to open the series to inspect the pixels."""
    core = AI_EVIDENCE_SERIES_DESCRIPTION
    if confidence is not None:
        return f"{core}: {label} p={confidence:.2f}"
    return f"{core}: {label}"


def _image_comment_with_label(label: str, tool_id: str, confidence: Optional[float]) -> str:
    """A DICOM-tag-length-friendly comment carrying the machine-readable evidence for a tag
    browser. Kept under 10240 chars per DICOM ImageComments VR (LT)."""
    if confidence is not None:
        return f"tool={tool_id} label={label!r} confidence={confidence:.4f}"
    return f"tool={tool_id} label={label!r}"


def _dcm_to_bytes(ds) -> bytes:
    """Serialise a pydicom Dataset to bytes we can POST to Orthanc as a DICOM file.

    Split out for testability -- callers assert on the serialised structure without an Orthanc
    round-trip.
    """
    import io
    from pydicom.filewriter import dcmwrite

    buf = io.BytesIO()
    # write_like_original=False adds the preamble + DICM magic that Orthanc's parser expects.
    dcmwrite(buf, ds, enforce_file_format=True)
    return buf.getvalue()


# --- lean projection for the Worklist API (issue #20) ------------------------
# Keep this consistent with `OrthancStableStudyEvent` field names — the Worklist
# API's response uses StudyInstanceUID as the primary key so a consumer can
# cross-reference with anything the orchestrator emitted.

def _lean_study(raw: dict) -> dict:
    """Project a raw /studies?expand=1 record to the lean shape the Worklist API
    serves. Only decision-relevant fields for the reader (no patient name / MRN
    / DOB — lean-reference: PHI minimization even inside our own network).

    Missing tags degrade to empty string rather than raising, so a partial
    Orthanc record does not knock a study off the worklist entirely."""
    main_tags = raw.get("MainDicomTags") or {}
    # numberOfInstances is intentionally not projected here. The /studies?expand
    # listing does not carry a Statistics block (instance counts live only on the
    # separate /studies/{id}/statistics endpoint), so sourcing it from this record
    # would always be null. A consumer that needs the count should fetch statistics
    # per study rather than rely on the single expand round-trip.
    return {
        "orthancStudyId":   raw.get("ID", ""),
        "studyInstanceUID": main_tags.get("StudyInstanceUID", ""),
        "accessionNumber":  main_tags.get("AccessionNumber", ""),
        "modality":         main_tags.get("ModalitiesInStudy") or main_tags.get("Modality", ""),
        "studyDescription": main_tags.get("StudyDescription", ""),
        "studyDate":        main_tags.get("StudyDate", ""),
        "lastUpdate":       raw.get("LastUpdate", ""),
    }
