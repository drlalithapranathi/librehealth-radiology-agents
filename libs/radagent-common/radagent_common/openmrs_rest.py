"""Thin OpenMRS REST (webservices.rest) client -- the NON-fhir2 escape hatch for lookups fhir2
cannot serve.

Today it does ONE thing: resolve a DICOM accession to its RadiologyOrder (#70). fhir2 exposes a
RadiologyOrder as ServiceRequest/<order uuid>, but it has NO searchable accession identifier --
`ServiceRequest?identifier=<accession>` returns HTTP 400 on the deployed fhir2 4.1.0 (verified
live), and the ServiceRequest resource carries no identifier at all. So the ingest-side #11 join
cannot turn an accession into a ServiceRequest ref through fhir2.

The radiology module's own REST search handler IS the authoritative accession index:
`GET /ws/rest/v1/radiologyorder?accessionNumber=<acc>` (RadiologyOrderSearchHandler). The order
uuid it returns is exactly the id fhir2 uses for the ServiceRequest, and it is the same uuid the
signed report's `basedOn` points at (see emitFhirDiagnosticReport in the sibling repo), so both
sides of the sign-off join land on the SAME ServiceRequest/<order uuid>.

Read-only. HTTP Basic, the same FHIR2_BASIC_* credentials the fhir2 client uses. Every lookup is
best-effort: like the fhir2 resolve it must never fail ingestion, so callers swallow errors and
fall back to Patient/UNRESOLVED.

TRANSPORT: this surface rides the SAME wire as fhir2 -- the base URL is derived from
FHIR2_BASE_URL and the same Basic credentials travel with every request, and the responses carry
patient/order identifiers. So it obeys the SAME read-transport guard (#67): plaintext HTTP to a
non-loopback host is refused unless the deployment opted in, exactly like a fhir2 read. Without
this, the fhir2 front door is locked while every DICOM arrival walks the credentials out this one.
"""
from __future__ import annotations
from typing import Any, Optional
import logging
import os
from urllib.parse import urlparse
import httpx

from .fhir_client import _guard_read_transport

_log = logging.getLogger("radagent_common.openmrs_rest")

# OpenMRS Order.urgency -> StudyContext order.priority (the triage signal, #61). The envelope pins
# priority to a four-value enum; anything unrecognised becomes "no priority" (honest, and it will
# not fail StudyContext validation).
_URGENCY_TO_PRIORITY = {"STAT": "stat", "ROUTINE": "routine", "ON_SCHEDULED_DATE": "routine"}


def _icd10_reason_codes(order_reason: Optional[dict]) -> list[str]:
    """The order reason Concept's ICD-10 codes, in mapping order, deduped -- or [].

    The module order reason is an OpenMRS Concept; what triage and the interpretation registry
    match on is the ICD-10 code (#81), so only the Concept's ICD-10 reference-term mappings
    travel (lean-reference: the code, never the free-text reason). Source names are dictionary
    conventions, not a spec -- the live CIEL dictionary says "ICD-10-WHO" -- so the filter
    normalises (upper, drop dashes/spaces) and takes any ICD10* source, which excludes
    "ICD-11-WHO" (normalises to ICD11...) and every non-ICD source. Malformed mapping shapes
    from a live dictionary contribute nothing rather than raising: the resolver is best-effort
    end to end, and a broken mapping must not cost the patient/order join.
    """
    codes: list[str] = []
    for mapping in (order_reason or {}).get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        term = mapping.get("conceptReferenceTerm") or {}
        if not isinstance(term, dict):
            continue
        source = (term.get("conceptSource") or {})
        name = source.get("name") if isinstance(source, dict) else None
        normalised = str(name or "").upper().replace("-", "").replace(" ", "")
        code = term.get("code")
        if normalised.startswith("ICD10") and isinstance(code, str) and code.strip():
            if code.strip() not in codes:
                codes.append(code.strip())
    return codes


def rest_base_url() -> str:
    """Public alias of `_default_rest_base` for other in-repo consumers (the worklist-api ack
    surface resolves acknowledger identity against `{base}/session`), so the derivation stays
    single-sourced instead of copied."""
    return _default_rest_base()


def _default_rest_base() -> str:
    """Derive the OpenMRS REST base from FHIR2_BASE_URL so no new env/compose wiring is needed:
    `.../ws/fhir2/R4` -> `.../ws/rest/v1`. Overridable with OPENMRS_REST_BASE_URL."""
    explicit = os.environ.get("OPENMRS_REST_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    fhir2 = os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4")
    parsed = urlparse(fhir2)
    # replace the fhir2 path segment with the REST one, keep scheme+host+the /openmrs prefix
    root = parsed.path.split("/ws/", 1)[0]  # ".../openmrs"
    return f"{parsed.scheme}://{parsed.netloc}{root}/ws/rest/v1"


def _basic_auth_from_env() -> Optional[tuple[str, str]]:
    """Reuse the fhir2 Basic credentials -- one account reads both surfaces. A half-set pair is a
    config error (like the fhir2 client), rejected loudly rather than silently unauthenticated."""
    user = os.environ.get("FHIR2_BASIC_USER")
    password = os.environ.get("FHIR2_BASIC_PASS")
    if bool(user) != bool(password):
        raise ValueError("FHIR2_BASIC_USER and FHIR2_BASIC_PASS must be set together")
    return (user, password) if user else None


class OpenmrsRestClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or _default_rest_base()).rstrip("/")
        self._timeout = timeout
        self._auth = _basic_auth_from_env()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        # Same invariant, same opt-ins as the fhir2 client (see module docstring). Raises before
        # any request leaves the process; ingress's best-effort swallow turns that into
        # Patient/UNRESOLVED with the reason in the warning, never a failed ingestion (#11).
        _guard_read_transport(url)
        async with httpx.AsyncClient(timeout=self._timeout, auth=self._auth) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def resolve_radiology_order_by_accession(self, accession: str) -> Optional[dict]:
        """accession -> the ingest's `patient`/`order` refs + priority, or None on no match.

        Returns the SAME shape as Fhir2Client.resolve_order_by_accession so ingress is agnostic:
            {"fhirPatientId": "Patient/<uuid>",
             "fhirServiceRequestId": "ServiceRequest/<order uuid>",
             "priority": "stat"}          # omitted when the order carries no mapped urgency

        `fhirServiceRequestId` is `ServiceRequest/<order uuid>` because fhir2 keys a RadiologyOrder's
        ServiceRequest on the order uuid -- so this ref equals the signed report's `basedOn`, closing
        the sign-off join. A custom rep keeps the response lean and avoids depending on the default
        representation's field set.

        `reasonCode` (#81) carries the order reason's ICD-10 codes only -- the module order reason
        is an OpenMRS Concept, and what triage and the interpretation registry's reason-code slice
        match on is the Concept's ICD-10 reference-term mapping ("rule out pneumothorax" -> J93*/
        J95.811 selects pneumothorax-detect on the ORDER, not just the pixels). An order whose
        reason has no ICD-10 mapping resolves without the key, exactly like an order with no
        reason (honest, and the pre-#81 shape). Lean-reference: codes travel, never free text.
        """
        if not accession:
            return None
        # The reason mappings ride the SAME request as the join -- but they must never COST the
        # join. The module converter materialises the rep per RESULT, so a deployment whose
        # radiologyorder rejects the nested orderReason rep answers 400 only once real orders
        # match -- and without this fallback that 400 would bubble into ingress' best-effort
        # swallow and degrade EVERY study to Patient/UNRESOLVED (losing the join AND priority
        # stack-wide, a #70/#61 regression far bigger than a missing reasonCode). On a 400 the
        # resolve retries once with the pre-#81 rep: join + priority always, codes when the
        # module can serve them. Any non-400 failure keeps its outage semantics (bubbles to the
        # caller's swallow, exactly as before).
        base_rep = "custom:(uuid,urgency,patient:(uuid))"
        reason_rep = ("custom:(uuid,urgency,patient:(uuid),"
                      "orderReason:(mappings:(conceptReferenceTerm:(code,conceptSource:(name)))))")
        try:
            bundle = await self._get(
                "radiologyorder", {"accessionNumber": accession, "v": reason_rep})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400:
                raise
            _log.warning(
                "radiologyorder rejected the orderReason rep (HTTP 400); resolving without "
                "reasonCode -- the #81 order-side trigger is OFF on this deployment")
            bundle = await self._get(
                "radiologyorder", {"accessionNumber": accession, "v": base_rep})
        for order in bundle.get("results", []) or []:
            order_uuid = order.get("uuid")
            patient_uuid = (order.get("patient") or {}).get("uuid")
            if order_uuid and patient_uuid:
                resolved = {
                    "fhirPatientId": f"Patient/{patient_uuid}",
                    "fhirServiceRequestId": f"ServiceRequest/{order_uuid}",
                }
                priority = _URGENCY_TO_PRIORITY.get(str(order.get("urgency") or "").upper())
                if priority:
                    resolved["priority"] = priority
                reason_codes = _icd10_reason_codes(order.get("orderReason"))
                if reason_codes:
                    resolved["reasonCode"] = reason_codes   # array of strings, per StudyContext
                return resolved
        return None
