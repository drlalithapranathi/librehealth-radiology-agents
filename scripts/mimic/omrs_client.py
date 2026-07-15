"""Write client for the MIMIC ETL (#68): the mixed loader the probes forced.

fhir2 cannot create the resources #68 needs most (ServiceRequest 400, MedicationRequest 400,
Encounter 400, Condition 500), so the ETL is a MIX, chosen per resource by what actually works on
the deployed stack:
  - fhir2 create: Patient? no (422 preferred-id). Observation yes. DiagnosticReport yes.
  - OpenMRS REST (webservices.rest): Patient, Encounter, Condition, drug orders.
  - direct SQL: the RadiologyOrder (module has no REST create; proven in the #70 E2E).

Connection config (env), defaulting to the in-compose-network names so the ETL can run as a one-shot
container beside the stack; override to localhost for host-run tooling:
  FHIR2_BASE_URL (default http://openmrs:8080/openmrs/ws/fhir2/R4), FHIR2_BASIC_USER/PASS
  OMRS_REST_BASE_URL (derived from FHIR2_BASE_URL if unset)
  OMRS_DB_HOST (mariadb) / OMRS_DB_PORT (3306) / OMRS_DB_USER (openmrs) / OMRS_DB_PASS (openmrs) / OMRS_DB_NAME (openmrs)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional
import os
from urllib.parse import urlparse
import httpx

# Stable references discovered on the o3 demo stack (overridable via env for another deployment).
RADIOLOGY_ORDER_TYPE_UUID = os.environ.get("MIMIC_ORDER_TYPE_UUID", "dbdb9a9b-56ea-11e5-a47f-08002719a237")
RADIOLOGY_CARE_SETTING_UUID = os.environ.get("MIMIC_CARE_SETTING_UUID", "6f0c9a92-6f24-11e3-af88-005056821db0")
RADIOLOGY_ENCOUNTER_TYPE_UUID = os.environ.get("MIMIC_ENCOUNTER_TYPE_UUID", "19db8c0d-3520-48f2-babd-77f2d450e5c7")
PATIENT_ID_TYPE_UUID = os.environ.get("MIMIC_PATIENT_ID_TYPE_UUID", "8d79403a-c2cc-11de-8d13-0010c6dffd0f")  # Old Identification Number (manual) -> holds subject_id
OPENMRS_ID_TYPE_UUID = os.environ.get("MIMIC_OPENMRS_ID_TYPE_UUID", "05a29f94-c0ed-11e2-94be-8c13b969e334")  # OpenMRS ID (required)
IDGEN_SOURCE_UUID = os.environ.get("MIMIC_IDGEN_SOURCE_UUID", "8549f706-7e85-4c1d-9424-217d50a2988b")  # Generator for OpenMRS ID
LOCATION_UUID = os.environ.get("MIMIC_LOCATION_UUID", "1ce1b7d4-c865-4178-82b0-5932e51503d6")  # Community Outreach
ENCOUNTER_ROLE_UUID = os.environ.get("MIMIC_ENCOUNTER_ROLE_UUID", "13fc9b4a-49ed-429c-9dde-ca005b387a3d")  # Radiology Ordering Provider
# The order/report concept. The demo dictionary has no CXR procedure, so the ETL provisions one
# (bootstrap_radiology_concept.py) and points this at it. No default: a wrong concept 500s fhir2.
ORDER_CONCEPT_UUID = os.environ.get("MIMIC_ORDER_CONCEPT_UUID", "")

_URGENCY = {"routine": "ROUTINE", "stat": "STAT", "urgent": "STAT", "asap": "STAT"}


@dataclass
class EtlConfig:
    fhir2_base: str = os.environ.get("FHIR2_BASE_URL", "http://openmrs:8080/openmrs/ws/fhir2/R4").rstrip("/")
    rest_base: str = ""
    basic: tuple[str, str] = (os.environ.get("FHIR2_BASIC_USER", "admin"),
                              os.environ.get("FHIR2_BASIC_PASS", "Admin123"))
    db_host: str = os.environ.get("OMRS_DB_HOST", "mariadb")
    db_port: int = int(os.environ.get("OMRS_DB_PORT", "3306"))
    db_user: str = os.environ.get("OMRS_DB_USER", "openmrs")
    db_pass: str = os.environ.get("OMRS_DB_PASS", "openmrs")
    db_name: str = os.environ.get("OMRS_DB_NAME", "openmrs")

    def __post_init__(self):
        if not self.rest_base:
            explicit = os.environ.get("OMRS_REST_BASE_URL")
            if explicit:
                self.rest_base = explicit.rstrip("/")
            else:
                p = urlparse(self.fhir2_base)
                root = p.path.split("/ws/", 1)[0]
                self.rest_base = f"{p.scheme}://{p.netloc}{root}/ws/rest/v1"


class OmrsClient:
    def __init__(self, cfg: Optional[EtlConfig] = None, admin_provider_uuid: Optional[str] = None):
        self.cfg = cfg or EtlConfig()
        self._http = httpx.Client(auth=self.cfg.basic, timeout=60.0)
        self._conn = None
        self._provider_uuid = admin_provider_uuid

    # --- transports ---------------------------------------------------------
    def _fget(self, path, params=None):
        r = self._http.get(f"{self.cfg.fhir2_base}/{path.lstrip('/')}", params=params); r.raise_for_status(); return r.json()

    def _fpost(self, res, body):
        r = self._http.post(f"{self.cfg.fhir2_base}/{res}", json=body); r.raise_for_status(); return r.json()

    def _fput(self, res, rid, body):
        r = self._http.put(f"{self.cfg.fhir2_base}/{res}/{rid}", json=body); r.raise_for_status(); return r.json()

    def _rpost(self, res, body):
        r = self._http.post(f"{self.cfg.rest_base}/{res}", json=body); r.raise_for_status(); return r.json()

    def _rget(self, res, params=None):
        r = self._http.get(f"{self.cfg.rest_base}/{res}", params=params); r.raise_for_status(); return r.json()

    def _db(self):
        if self._conn is None:
            import pymysql
            self._conn = pymysql.connect(host=self.cfg.db_host, port=self.cfg.db_port,
                                         user=self.cfg.db_user, password=self.cfg.db_pass,
                                         database=self.cfg.db_name, autocommit=True)
        return self._conn

    def _id_by_uuid(self, table: str, id_col: str, uuid: str) -> Optional[int]:
        with self._db().cursor() as c:
            c.execute(f"select {id_col} from {table} where uuid=%s", (uuid,))
            row = c.fetchone()
            return row[0] if row else None

    def provider_uuid(self) -> str:
        if not self._provider_uuid:
            self._provider_uuid = self._rget("provider", {"v": "custom:(uuid)", "limit": 1})["results"][0]["uuid"]
        return self._provider_uuid

    # --- creates ------------------------------------------------------------
    def _generate_openmrs_id(self) -> str:
        """OpenMRS ID is a required identifier and is normally minted by idgen; the raw patient REST
        does not auto-generate it, so we mint one here."""
        return self._rpost(f"idgen/identifiersource/{IDGEN_SOURCE_UUID}/identifier", {})["identifier"]

    def create_patient(self, subject_id: str, gender: str = "U", birthdate: str = "1970-01-01",
                        given: str = "MIMIC", family: str = "Subject") -> str:
        """Get-or-create by subject_id (the ETL must be re-runnable, #68 criterion 2). fhir2 rejects
        create without a preferred id, so this is OpenMRS REST, with two identifiers: the required
        OpenMRS ID (minted, preferred) and the MIMIC subject_id (so the EHR loader finds it again)."""
        existing = self.find_patient_by_subject_id(subject_id)
        if existing:
            return existing
        body = {
            "identifiers": [
                {"identifier": self._generate_openmrs_id(), "identifierType": OPENMRS_ID_TYPE_UUID,
                 "location": LOCATION_UUID, "preferred": True},
                {"identifier": str(subject_id), "identifierType": PATIENT_ID_TYPE_UUID,
                 "location": LOCATION_UUID, "preferred": False},
            ],
            "person": {"names": [{"givenName": given, "familyName": f"{family}{subject_id}"}],
                       "gender": (gender or "U")[:1].upper(), "birthdate": birthdate},
        }
        return self._rpost("patient", body)["uuid"]

    def find_patient_by_subject_id(self, subject_id: str) -> Optional[str]:
        res = self._rget("patient", {"q": str(subject_id), "v": "custom:(uuid,identifiers:(identifier))"})
        for p in res.get("results", []):
            if any(i.get("identifier") == str(subject_id) for i in p.get("identifiers", [])):
                return p["uuid"]
        return None

    def create_encounter(self, patient_uuid: str, when_iso: str) -> str:
        body = {"patient": patient_uuid, "encounterType": RADIOLOGY_ENCOUNTER_TYPE_UUID,
                "location": LOCATION_UUID, "encounterDatetime": when_iso,
                "encounterProviders": [{"provider": self.provider_uuid(),
                                        "encounterRole": ENCOUNTER_ROLE_UUID}]}
        return self._rpost("encounter", body)["uuid"]

    def insert_radiology_order(self, patient_uuid: str, encounter_uuid: str, accession: str,
                               concept_uuid: str, priority: str = "routine",
                               order_number: Optional[str] = None) -> str:
        """Insert the three rows that make a RadiologyOrder (orders + test_order + radiology_order),
        the path proven in the #70 E2E. Returns the order uuid == the fhir2 ServiceRequest id the
        signed report's basedOn must point at. Idempotent by accession: returns the existing order."""
        db = self._db()
        with db.cursor() as c:
            c.execute("select o.uuid from orders o join radiology_order r on r.order_id=o.order_id "
                      "where o.accession_number=%s and o.voided=0 limit 1", (accession,))
            row = c.fetchone()
            if row:
                return row[0]
        # NB: the `patient` table has no uuid column -- a Patient IS-A Person and the uuid lives on
        # `person`, where person_id == patient_id.
        pid = self._id_by_uuid("person", "person_id", patient_uuid)
        eid = self._id_by_uuid("encounter", "encounter_id", encounter_uuid)
        cid = self._id_by_uuid("concept", "concept_id", concept_uuid)
        otid = self._id_by_uuid("order_type", "order_type_id", RADIOLOGY_ORDER_TYPE_UUID)
        csid = self._id_by_uuid("care_setting", "care_setting_id", RADIOLOGY_CARE_SETTING_UUID)
        prov = self._id_by_uuid("provider", "provider_id", self.provider_uuid())
        missing = [n for n, v in [("patient", pid), ("encounter", eid), ("concept", cid),
                                  ("order_type", otid), ("care_setting", csid), ("provider", prov)] if not v]
        if missing:
            raise ValueError(f"cannot insert order for accession {accession}: unresolved {missing}")
        onum = order_number or f"MIMIC-{accession}"
        urgency = _URGENCY.get(priority.lower(), "ROUTINE")
        with db.cursor() as c:
            c.execute(
                "insert into orders (uuid, order_number, order_action, concept_id, patient_id, "
                "encounter_id, orderer, care_setting, order_type_id, urgency, accession_number, "
                "date_activated, date_created, creator, voided) values "
                "(UUID(), %s, 'NEW', %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), 1, 0)",
                (onum, cid, pid, eid, prov, csid, otid, urgency, accession))
            oid = c.lastrowid
            c.execute("insert into test_order (order_id) values (%s)", (oid,))
            c.execute("insert into radiology_order (order_id) values (%s)", (oid,))
            c.execute("select uuid from orders where order_id=%s", (oid,))
            return c.fetchone()[0]

    def create_observation(self, patient_uuid: str, concept_uuid: str, value: float,
                           unit: str, when_iso: str) -> str:
        body = {"resourceType": "Observation", "status": "final",
                "code": {"coding": [{"code": concept_uuid}]},
                "subject": {"reference": f"Patient/{patient_uuid}"},
                "effectiveDateTime": when_iso,
                "valueQuantity": {"value": value, "unit": unit}}
        return self._fpost("Observation", body)["id"]

    def create_condition(self, patient_uuid: str, concept_uuid: str, onset_iso: str) -> str:
        """OpenMRS REST /condition (fhir2 Condition create 500s here). The problem is a coded
        Concept; onset stamps it. NOTE: the coded-concept shape varies by OpenMRS build -- verify
        against the provisioned dictionary when loading real MIMIC-IV diagnoses_icd (#68)."""
        body = {"patient": patient_uuid,
                "condition": {"coded": concept_uuid},
                "clinicalStatus": "ACTIVE",
                "onsetDate": onset_iso}
        return self._rpost("condition", body)["uuid"]

    def seed_diagnostic_report(self, patient_uuid: str, service_request_uuid: str,
                               concept_uuid: str, conclusion: str, status: str = "preliminary") -> str:
        body = {"resourceType": "DiagnosticReport", "status": status,
                "code": {"coding": [{"code": concept_uuid}], "text": "Radiology report"},
                "subject": {"reference": f"Patient/{patient_uuid}"},
                "basedOn": [{"reference": f"ServiceRequest/{service_request_uuid}"}],
                "conclusion": conclusion}
        return self._fpost("DiagnosticReport", body)["id"]

    def finalize_diagnostic_report(self, report_id: str) -> str:
        """Flip a seeded preliminary report to final so the RIS poller fires report_finalized
        (the flip-to-final rehearsal cue). Re-PUTs the resource with status=final."""
        r = self._fget(f"DiagnosticReport/{report_id}")
        r["status"] = "final"
        return self._fput("DiagnosticReport", report_id, r)["id"]

    def order_for_accession(self, accession: str) -> Optional[dict]:
        """The module's accession index (what the orchestrator ingest resolver uses): the order uuid
        (== the fhir2 ServiceRequest id) and its patient uuid, or None."""
        res = self._rget("radiologyorder", {"accessionNumber": accession,
                                             "v": "custom:(uuid,patient:(uuid))"})
        for o in res.get("results", []):
            if o.get("uuid") and (o.get("patient") or {}).get("uuid"):
                return {"order_uuid": o["uuid"], "patient_uuid": o["patient"]["uuid"]}
        return None

    def find_seeded_report(self, patient_uuid: str, service_request_uuid: str) -> Optional[str]:
        """Our seeded DiagnosticReport for this order (basedOn the ServiceRequest), any status."""
        bundle = self._fget("DiagnosticReport", {"subject": f"Patient/{patient_uuid}"})
        want = f"ServiceRequest/{service_request_uuid}"
        for e in bundle.get("entry", []) or []:
            r = e.get("resource") or {}
            if any((b.get("reference") == want) for b in (r.get("basedOn") or [])):
                return r.get("id")
        return None

    def close(self):
        self._http.close()
        if self._conn:
            self._conn.close()
