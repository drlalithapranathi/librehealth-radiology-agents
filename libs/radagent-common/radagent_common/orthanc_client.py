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
