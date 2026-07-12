"""Thin OpenMRS fhir2 (FHIR R4) client. Mostly READ-ONLY (see architecture notes: Risk R1);
`write_presign_impression` (#26) is the one write path, confirmed feasible by the R1 spike
(DiagnosticReport create/update is wired on the live fhir2 build).

Methods are stubs for M0; wire to the live fhir2 base URL in M1. Every agent that needs
clinical data uses THIS client (lean-reference: fetch from source, do not pass PHI in messages).
"""
from __future__ import annotations
from typing import Any, Optional
import os
import httpx


def _basic_auth_from_env() -> Optional[tuple[str, str]]:
    """(user, pass) for live fhir2, or None to stay unauthenticated (mocks, unit tests).

    Live fhir2 401s every unauthenticated read — and callers deliberately swallow fhir2 errors
    to protect ingestion, so a missing credential shows up as silence, not a crash (#53). A
    half-set pair is therefore rejected loudly here rather than silently downgraded. The values
    themselves must never be logged.
    """
    user = os.environ.get("FHIR2_BASIC_USER")
    password = os.environ.get("FHIR2_BASIC_PASS")
    if bool(user) != bool(password):
        raise ValueError("FHIR2_BASIC_USER and FHIR2_BASIC_PASS must be set together")
    return (user, password) if user else None


class Fhir2Client:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4")).rstrip("/")
        self._timeout = timeout
        self._auth = _basic_auth_from_env()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        # `path` may be a relative resource ("DiagnosticReport") or an absolute Bundle next-page URL.
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, resource: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.post(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    async def _put(self, path: str, resource: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.put(f"{self.base_url}/{path.lstrip('/')}", json=resource)
            r.raise_for_status()
            return r.json()

    # --- read helpers used by EHR Assistant / orchestrator ------------------

    async def get_patient(self, fhir_patient_id: str) -> Optional[dict]:
        """GET Patient/{id} — accepts either the bare id ('demo-1') or the reference
        form ('Patient/demo-1'). Returns None if the patient is not found (404).

        Kept for other agents' use; the EHR Assistant itself does not consume patient
        demographics (its output surfaces refs + codes, not name / DOB — lean-reference)."""
        if not fhir_patient_id:
            return None
        ref = fhir_patient_id if "/" in fhir_patient_id else f"Patient/{fhir_patient_id}"
        try:
            return await self._get(ref)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def search_imaging_studies(self, fhir_patient_id: str) -> list[dict]:
        """GET ImagingStudy?patient=... — prior imaging for context.

        Returns a lean list ({ref, modality, date}) matching the priorStudies items in
        `contracts/skills/ehr.schema.json`. `reportRef` is deliberately NOT populated;
        linking each ImagingStudy to its DiagnosticReport needs a `_revinclude` spike
        against the live OpenMRS fhir2 (M2). Follows Bundle `next` links.
        """
        return [_lean_imaging_study(r) for r in await self._collect(
            "ImagingStudy", {"patient": _patient_query(fhir_patient_id)})]

    async def search_observations(self, fhir_patient_id: str, codes: list[str]) -> list[dict]:
        """GET Observation?patient=...&code=<loinc-csv> — latest observations for the
        requested LOINCs. Codes are joined with `,` per FHIR search syntax so a single
        request covers the whole panel (creatinine + every eGFR LOINC variant we care
        about for contrast decisions).

        Returns lean records ({code, display, value, unit, date}) matching the
        relevantLabs items in `contracts/skills/ehr.schema.json`. Empty `codes` -> [].
        """
        if not codes:
            return []
        return [_lean_observation(r) for r in await self._collect(
            "Observation",
            {"patient": _patient_query(fhir_patient_id), "code": ",".join(codes)})]

    async def search_conditions(self, fhir_patient_id: str) -> list[dict]:
        """GET Condition?patient=...&clinical-status=active — problem list.

        Returns lean records ({code, display}) matching the activeProblems items in
        the schema. Client-side filters out non-active clinical statuses in case the
        server ignores the `clinical-status` search parameter (OpenMRS fhir2 has
        historically had gaps — see `poll_finalized_reports` for the status/400 case)."""
        entries = await self._collect(
            "Condition",
            {"patient": _patient_query(fhir_patient_id), "clinical-status": "active"})
        return [_lean_condition(r) for r in entries if _condition_is_active(r)]

    async def search_allergies(self, fhir_patient_id: str) -> list[dict]:
        """GET AllergyIntolerance?patient=... — allergies + criticality.

        Returns lean records ({code, criticality}) matching the allergies items in
        the schema. `code` is the primary coding value (RxNorm / SNOMED / other) — the
        first coded system found is used, freeing downstream from parsing FHIR
        CodeableConcept structure."""
        return [_lean_allergy(r) for r in await self._collect(
            "AllergyIntolerance", {"patient": _patient_query(fhir_patient_id)})]

    async def search_medications(self, fhir_patient_id: str) -> list[dict]:
        """GET MedicationRequest?patient=... — current medications.

        Returns lean records ({code, display}) — one per active med — used by the
        EHR Assistant to derive medicationFlags (onMetformin / onAnticoagulant / etc.)
        via RxNorm code match with a case-insensitive text fallback. The `status` search
        param is deliberately NOT sent: live fhir2 returns 500 on
        `MedicationRequest?status=...` (a server NPE, verified against the o3 fhir2 build),
        which the caller would swallow into an empty slice, silently disabling every med
        flag. Activeness is filtered client-side via `_medication_is_active` instead — the
        same status-param-avoidance `resolve_order_by_accession` uses."""
        entries = await self._collect(
            "MedicationRequest",
            {"patient": _patient_query(fhir_patient_id)})
        return [_lean_medication(r) for r in entries if _medication_is_active(r)]

    async def _collect(self, path: str, params: dict[str, Any]) -> list[dict]:
        """Follow Bundle `next` links and yield every resource entry — the common
        paging idiom shared by every search_* method above. Matches the paging
        behavior of `poll_finalized_reports` so all searches degrade the same way
        under an OpenMRS with paged responses."""
        collected: list[dict] = []
        target: Optional[str] = path
        current_params: dict[str, Any] | None = params
        while target:
            try:
                bundle = await self._get(target, current_params)
            except httpx.HTTPStatusError as e:
                # A resource type the fhir2 build does not expose (e.g. ImagingStudy on the
                # o3 image) answers 404. Treat "not found / unsupported" as no results rather
                # than raising, so one unavailable slice degrades to [] the way get_patient's
                # 404 degrades to None — instead of failing the whole search.
                if e.response.status_code == 404:
                    return collected
                raise
            for entry in bundle.get("entry", []) or []:
                resource = entry.get("resource") or {}
                if resource:
                    collected.append(resource)
            target, current_params = _bundle_next_link(bundle), None  # next link is absolute
        return collected

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

    async def write_presign_impression(
        self, service_request_ref: str, patient_ref: str, impression_text: str,
    ) -> str:
        """Offer the pre-sign draft impression into the RIS as a `preliminary` DiagnosticReport
        (issue #26) -- advisory only, never transitioned to `final`; the radiologist's own signed
        report is a separate `final` DiagnosticReport the RIS creates on sign-off.

        Idempotent per order: a pre-sign re-run (e.g. more aiFindings tools complete) updates the
        SAME draft instead of accumulating duplicates, found via `_find_presign_draft`. `status`
        is deliberately NOT sent as a search param -- the #3 spike found `DiagnosticReport?status=`
        400s on live fhir2 -- so the preliminary/final distinction is filtered client-side there,
        same as `poll_finalized_reports`.

        Returns the written DiagnosticReport's bare id.
        """
        existing_id = await self._find_presign_draft(service_request_ref)
        resource = {
            "resourceType": "DiagnosticReport",
            "status": "preliminary",
            "code": {"text": "AI pre-sign impression draft"},
            "subject": {"reference": patient_ref},
            "basedOn": [{"reference": service_request_ref}],
            "conclusion": impression_text,
        }
        if existing_id:
            resource["id"] = existing_id
            written = await self._put(f"DiagnosticReport/{existing_id}", resource)
        else:
            written = await self._post("DiagnosticReport", resource)
        return written["id"]

    async def _find_presign_draft(self, service_request_ref: str) -> Optional[str]:
        """The `preliminary` DiagnosticReport a prior `write_presign_impression` call already
        created for this order, if any -- the idempotency key an update reuses instead of
        duplicating. Searches by `based-on` only (see `write_presign_impression` on `status`)."""
        bundle = await self._get("DiagnosticReport", {"based-on": service_request_ref})
        for entry in bundle.get("entry", []) or []:
            resource = entry.get("resource") or {}
            if resource.get("resourceType") == "DiagnosticReport" and resource.get("status") == "preliminary":
                return resource.get("id")
        return None

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


# --- lean projections for EHR context assembly (issue #4) ----------------
# Each helper takes a raw FHIR resource (as returned by fhir2) and returns the
# decision-relevant slice that matches the corresponding items schema in
# `contracts/skills/ehr.schema.json`. NO raw record dumps — refs, codes, values
# only (lean-reference: PHI minimization).


def _patient_query(fhir_patient_id: str) -> str:
    """FHIR search accepts either a bare id or a reference; normalize to the bare id
    since some OpenMRS builds reject the reference form on `patient=` search params."""
    return fhir_patient_id.split("/", 1)[1] if "/" in fhir_patient_id else fhir_patient_id


def _first_coding_value(codeable_concept: dict) -> tuple[Optional[str], Optional[str]]:
    """Pull the first (code, display) pair from a FHIR CodeableConcept. Callers use
    this to avoid parsing FHIR CodeableConcept structure in every lean projector."""
    for coding in (codeable_concept or {}).get("coding", []) or []:
        if isinstance(coding, dict) and coding.get("code"):
            return coding["code"], coding.get("display")
    return None, (codeable_concept or {}).get("text")


def _lean_imaging_study(resource: dict) -> dict:
    """ImagingStudy -> {ref, modality, date}. `modality` on ImagingStudy is a list of
    Coding under `modality[]`; we take the first code. `date` is `started` (per FHIR R4)."""
    modality_codings = resource.get("modality") or []
    modality = (modality_codings[0].get("code") if (modality_codings
                and isinstance(modality_codings[0], dict)) else None) or ""
    out: dict = {"ref": f"ImagingStudy/{resource.get('id')}"}
    if modality:
        out["modality"] = modality
    started = resource.get("started")
    if started:
        out["date"] = started
    return out


def _lean_observation(resource: dict) -> dict:
    """Observation -> {code, display, value?, unit?, date?}. Handles `valueQuantity` and
    `valueString` — the two commonest lab shapes. Missing pieces are simply omitted so
    schema `required: ["code"]` is met and optionals only appear when known."""
    code, display = _first_coding_value(resource.get("code") or {})
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    if "valueQuantity" in resource:
        vq = resource["valueQuantity"] or {}
        if "value" in vq:
            out["value"] = vq["value"]
        if vq.get("unit"):
            out["unit"] = vq["unit"]
    elif "valueString" in resource:
        out["value"] = resource["valueString"]
    date = resource.get("effectiveDateTime") or resource.get("issued")
    if date:
        out["date"] = date
    return out


def _condition_is_active(resource: dict) -> bool:
    """Client-side re-filter: keep only Conditions whose clinicalStatus is 'active'.
    The server search may or may not honor `clinical-status=active` (OpenMRS fhir2 has
    documented gaps — see the #3 spike). Conditions without any clinicalStatus at all
    are treated as active (defensive: better to surface than to hide)."""
    clinical_status = resource.get("clinicalStatus") or {}
    codings = clinical_status.get("coding") or []
    if not codings:
        return True
    return any(isinstance(c, dict) and c.get("code") == "active" for c in codings)


def _lean_condition(resource: dict) -> dict:
    """Condition -> {code, display}. First coding on the `code` CodeableConcept."""
    code, display = _first_coding_value(resource.get("code") or {})
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    return out


def _lean_allergy(resource: dict) -> dict:
    """AllergyIntolerance -> {code, criticality}. `criticality` is a native FHIR field
    (low | high | unable-to-assess); omitted if absent."""
    code, _ = _first_coding_value(resource.get("code") or {})
    out: dict = {"code": code or ""}
    criticality = resource.get("criticality")
    if criticality:
        out["criticality"] = criticality
    return out


def _medication_is_active(resource: dict) -> bool:
    return (resource.get("status") or "").lower() == "active"


def _lean_medication(resource: dict) -> dict:
    """MedicationRequest -> {code, display} projected from `medicationCodeableConcept`
    (the inline-code form; a `medicationReference` would need a follow-up GET which we
    do not do — those come through with an empty code and are filtered downstream by
    the medicationFlags matcher when nothing matches)."""
    med_cc = resource.get("medicationCodeableConcept") or {}
    code, display = _first_coding_value(med_cc)
    out: dict = {"code": code or ""}
    if display:
        out["display"] = display
    return out
