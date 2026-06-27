"""Thin OpenMRS fhir2 (FHIR R4) client. v1 = READ-ONLY (see architecture notes: Risk R1).

Methods are stubs for M0; wire to the live fhir2 base URL in M1. Every agent that needs
clinical data uses THIS client (lean-reference: fetch from source, do not pass PHI in messages).
"""
from __future__ import annotations
from typing import Any, Optional
import os
import httpx


class Fhir2Client:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4")).rstrip("/")
        self._timeout = timeout

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}", params=params)
            r.raise_for_status()
            return r.json()

    # --- read helpers used by EHR Assistant / orchestrator (TODO(M1): implement real queries) ---
    async def get_patient(self, fhir_patient_id: str) -> dict:
        raise NotImplementedError("TODO(M1): GET Patient/{id}")

    async def search_imaging_studies(self, fhir_patient_id: str) -> list[dict]:
        raise NotImplementedError("TODO(M1): GET ImagingStudy?patient=...")

    async def search_observations(self, fhir_patient_id: str, codes: list[str]) -> list[dict]:
        raise NotImplementedError("TODO(M1): GET Observation?patient=...&code=...")

    async def poll_finalized_reports(self, since_iso: str) -> list[dict]:
        """RIS sign-off detection: DiagnosticReport?status=final&_lastUpdated=gt{since}."""
        raise NotImplementedError("TODO(M1): GET DiagnosticReport?status=final&_lastUpdated=gt...")
