"""Thin Orthanc REST client. Used for imaging metadata (lean-reference)."""
from __future__ import annotations
from typing import Any, Optional
import os
import httpx


class OrthancClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("ORTHANC_BASE_URL", "http://orthanc:8042")).rstrip("/")
        self._timeout = timeout

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
            r.raise_for_status()
            return r.json()

    async def get_study(self, orthanc_study_id: str) -> dict:
        return await self._get(f"studies/{orthanc_study_id}")

    async def get_study_description(self, orthanc_study_id: str) -> str:
        """The study's DICOM StudyDescription, or "" when Orthanc has no such tag (#62).

        This is the field the interpretation tool registry selects on (`select_tools(modality,
        description)`), and the Orthanc stable event does not carry it -- so ingress fetches it
        here. Imaging metadata comes from Orthanc, which is what Golden rule 2 asks for; keeping the
        DICOM tag name on this side of the client is the point of the client.

        Not PHI: StudyDescription is the protocol ("CT HEAD WITHOUT CONTRAST"), not the patient.
        """
        raw = await self.get_study(orthanc_study_id)
        main_tags = (raw or {}).get("MainDicomTags") or {}
        return (main_tags.get("StudyDescription") or "").strip()

    async def _get_bytes(self, path: str) -> bytes:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}")
            r.raise_for_status()
            return r.content

    async def list_study_instances(self, orthanc_study_id: str) -> list[str]:
        """Every instance (SOP) id in a study, in acquisition order (#27).

        The first pixel-level read path in the system: until now this client served only imaging
        METADATA (golden rule 2's "imaging metadata from Orthanc"), because no tool read pixels.
        A real CAD tool does, and the pixels must still come through this client rather than an
        agent talking to Orthanc directly.

        Orthanc models study -> series -> instance, and /studies/{id} carries only the series ids,
        so this walks one level down. Series are sorted by SeriesNumber and instances by
        InstanceNumber where those tags exist, so "the first instance" is deterministic rather than
        whatever order Orthanc happened to return -- a CXR tool that grabs an arbitrary frame of a
        two-view study is not reproducible, and a demo that silently picks the lateral is worse.
        """
        study = await self.get_study(orthanc_study_id)
        series_ids = (study or {}).get("Series") or []
        series: list[tuple[int, str, list[str]]] = []
        for sid in series_ids:
            raw = await self._get(f"series/{sid}")
            tags = (raw or {}).get("MainDicomTags") or {}
            series.append((_as_int(tags.get("SeriesNumber")), sid, (raw or {}).get("Instances") or []))
        series.sort(key=lambda s: (s[0], s[1]))

        out: list[str] = []
        for _, _, instance_ids in series:
            numbered: list[tuple[int, str]] = []
            for iid in instance_ids:
                raw = await self._get(f"instances/{iid}")
                tags = (raw or {}).get("MainDicomTags") or {}
                numbered.append((_as_int(tags.get("InstanceNumber")), iid))
            numbered.sort()
            out.extend(iid for _, iid in numbered)
        return out

    async def get_instance_dicom(self, instance_id: str) -> bytes:
        """The raw DICOM Part-10 file for one instance (#27).

        Raw bytes, not Orthanc's /preview PNG: preview is 8-bit and already windowed, and a CAD
        model that expects the original bit depth would be scoring a picture of the image rather
        than the image. Decoding is radagent_common.imaging's job, not this client's -- keeping the
        transport here and the pixel semantics there.

        THIS PAYLOAD IS PHI. It is pixel data with the patient's identity in its own header. It must
        never enter an A2A message (golden rule 2, lean-reference): an agent fetches it from Orthanc
        at the moment it needs it, scores it, and passes forward only the derived finding.
        """
        return await self._get_bytes(f"instances/{instance_id}/file")

    async def list_completed_studies(self) -> list[dict]:
        """Used by the Worklist API to build the reading worklist.

        Returns every study Orthanc knows about, in a lean shape suitable for
        joining with priority and assignment. Orthanc only exposes studies once
        their instances have finished landing (subject to StableAge on the OP
        side), so "listed" is effectively "completed / stable enough to read"
        for the reading-worklist use case.

        Uses `/studies?expand=1` for the list-with-details in a single round
        trip: at 200-ish stabilized studies the payload is well under a MB.
        If Orthanc grows the study count past ~10k in production, swap this
        for `/tools/find` with paged Query — the return shape stays the same.
        """
        raw = await self._get("studies", params={"expand": True})
        return [_lean_study(s) for s in raw or []]


def _as_int(v: Any) -> int:
    """DICOM SeriesNumber/InstanceNumber are IS (integer-string) and are frequently absent.

    A missing or non-numeric value sorts LAST rather than raising or sorting as 0 -- an untagged
    series must not silently become "series 1" and win the first-instance pick.
    """
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 1 << 30


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
