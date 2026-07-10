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
        # `path` may be a relative resource ("DiagnosticReport") or an absolute Bundle next-page URL.
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    # --- read helpers used by EHR Assistant / orchestrator (TODO(M1): implement real queries) ---
    async def get_patient(self, fhir_patient_id: str) -> dict:
        raise NotImplementedError("TODO(M1): GET Patient/{id}")

    async def search_imaging_studies(self, fhir_patient_id: str) -> list[dict]:
        raise NotImplementedError("TODO(M1): GET ImagingStudy?patient=...")

    async def search_observations(self, fhir_patient_id: str, codes: list[str]) -> list[dict]:
        raise NotImplementedError("TODO(M1): GET Observation?patient=...&code=...")

    async def resolve_order_by_accession(self, accession: str) -> Optional[dict]:
        """Resolve a DICOM accession to its patient + order refs (issue #11).

        Searches ServiceRequest by its accession identifier and returns the lean join refs the
        ingress needs to replace the `Patient/UNRESOLVED` placeholder:
            {"fhirPatientId": "Patient/<id>", "fhirServiceRequestId": "ServiceRequest/<id>"}
        Returns None when nothing matches. Read-only (a search GET); the refs are the only data
        that leave fhir2 -- no name or clinical content (lean-reference).

        NOTE: matches the accession as a bare FHIR `identifier` value (any system). If the live
        fhir2 needs the ACSN system pinned (`identifier=<system>|<value>`), narrow it here once the
        deployed OpenMRS is confirmed.
        """
        if not accession:
            return None
        bundle = await self._get("ServiceRequest", {"identifier": accession})
        for entry in bundle.get("entry", []) or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") != "ServiceRequest":
                continue
            patient_ref = (resource.get("subject") or {}).get("reference")
            sr_id = resource.get("id")
            if patient_ref and sr_id:
                return {"fhirPatientId": patient_ref,
                        "fhirServiceRequestId": f"ServiceRequest/{sr_id}"}
        return None

    async def get_report_conclusion(self, diagnostic_report_id: str) -> Optional[str]:
        """Fetch a finalized report's narrative conclusion by id (issue #16).

        The `ris.report.finalized` event is lean (IDs + refs only, no narrative -- Golden rule 2),
        so Impression Generation reads the report CONTENT from source: GET DiagnosticReport/<id>
        and return its `conclusion` (the radiologist's summary the impression structures from).
        Returns None when the id is empty, the report is missing, or it carries no conclusion.
        Read-only. The conclusion is the one clinical field the impression is entitled to consume.
        """
        if not diagnostic_report_id:
            return None
        ref = diagnostic_report_id if "/" in diagnostic_report_id else f"DiagnosticReport/{diagnostic_report_id}"
        resource = await self._get(ref)
        conclusion = resource.get("conclusion")
        return conclusion if isinstance(conclusion, str) and conclusion.strip() else None

    async def poll_finalized_reports(self, since_iso: str) -> tuple[list[dict], Optional[str]]:
        """RIS sign-off detection. Returns (finalized records oldest-first, high-water cursor).

        `status` is NOT a searchable param on the live fhir2 (OpenMRS 5.7.9) —
        `DiagnosticReport?status=final` returns 400 (verified in the #3 spike) — so we page by
        `_lastUpdated` and filter `status == final` client-side.

        Correctness of the cursor (issue #12 acceptance):
          * query `ge` (INCLUSIVE) + dedup by id in the poller, so a report sharing the boundary
            second is never lost to strict-greater (OpenMRS timestamps are second-precision);
          * follow every Bundle `next` link, so nothing is missed past page 1;
          * high-water = max `meta.lastUpdated` across ALL entries seen (any status, computed by
            max not by trusting `_sort`), so the poller advances past non-final reports too.
        Records are lean + PHI-free (IDs + refs + cursor).
        """
        reports: list[dict] = []
        high_water: Optional[str] = None
        target: Optional[str] = "DiagnosticReport"
        params: dict[str, Any] | None = {"_lastUpdated": f"ge{since_iso}", "_sort": "_lastUpdated"}
        while target:
            bundle = await self._get(target, params)
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") or {}
                if resource.get("resourceType") != "DiagnosticReport":
                    continue
                updated = (resource.get("meta") or {}).get("lastUpdated")
                if updated and (high_water is None or updated > high_water):
                    high_water = updated
                if resource.get("status") == "final":
                    reports.append(finalized_report_record(resource))
            target, params = _bundle_next_link(bundle), None  # next link is an absolute URL
        return reports, high_water


def _bundle_next_link(bundle: dict) -> Optional[str]:
    """The absolute URL of the Bundle's `next` page, if the server paged the result."""
    for link in bundle.get("link", []) or []:
        if isinstance(link, dict) and link.get("relation") == "next":
            return link.get("url")
    return None


def finalized_report_record(report: dict) -> dict:
    """Project a FHIR DiagnosticReport to the lean, PHI-free record the RIS poller signals:
    IDs + join refs + the `_lastUpdated` cursor. No patient name or clinical content."""
    meta = report.get("meta") or {}
    return {
        "diagnosticReportId": f"DiagnosticReport/{report.get('id')}",
        "status": report.get("status"),
        "serviceRequestRef": _based_on_service_request(report),
        "accessionNumber": _accession_number(report),
        "signedAt": report.get("issued"),
        "lastUpdatedCursor": meta.get("lastUpdated"),
    }


def _based_on_service_request(report: dict) -> Optional[str]:
    """The order the report was based on (the robust join #11 resolves at ingest)."""
    for based_on in report.get("basedOn", []) or []:
        reference = based_on.get("reference", "") if isinstance(based_on, dict) else ""
        if "ServiceRequest/" in reference:
            return reference
    return None


def _accession_number(report: dict) -> Optional[str]:
    """Accession usually rides as a FHIR identifier of type ACSN (the join we have at ingest)."""
    for ident in report.get("identifier", []) or []:
        if not isinstance(ident, dict):
            continue
        codings = ((ident.get("type") or {}).get("coding")) or []
        if any(isinstance(c, dict) and c.get("code") == "ACSN" for c in codings):
            return ident.get("value")
    return None
