# MIMIC-CXR showcase ETL: design + write paths (#68)

MIMIC replaces EMBED for the M4 demo (`docs/embed-mammography-mapping.md` stays the structural
template). This doc is the corrected mapping, because the live stack forced a different loader shape
than "POST FHIR bundles to fhir2".

## Write paths (probed live against the o3 stack, 2026-07-15)

fhir2 cannot create the resources the ETL needs most. What actually works, per resource:

| Resource | Mechanism | Note |
|---|---|---|
| Patient | OpenMRS REST `/patient` | needs a minted OpenMRS ID (idgen) + subject_id as a 2nd identifier; fhir2 create 422s |
| Encounter | OpenMRS REST `/encounter` | radiology encounter type + "Radiology Ordering Provider" role |
| **RadiologyOrder** | **direct SQL** (orders + test_order + radiology_order) | fhir2 ServiceRequest create 400s; module has NO REST create. Proven in the #70 E2E |
| Observation (labs) | fhir2 `/Observation` | needs a NUMERIC concept + a FHIR-valid instant (`+00:00`, not `+0000`) |
| Condition (problems) | OpenMRS REST `/condition` | fhir2 Condition create 500s |
| MedicationRequest (meds) | (none yet) | fhir2 400s; OpenMRS drug orders need a drug-concept model -- follow-up |
| DiagnosticReport (report) | fhir2 `/DiagnosticReport` | seed `preliminary` basedOn the order, flip to `final` |

## The join, end to end (why the order must be a module RadiologyOrder)

Post-#70 the orchestrator ingest resolves a study's order via the module's
`radiologyorder?accessionNumber=` (fhir2 cannot search ServiceRequest by accession). So the order
MUST be a module RadiologyOrder, and one accession value ties everything:

```
DICOM AccessionNumber  ==  orders.accession_number  ==  study_id (e.g. s56699142)
        |                          |                           |
  dicom_fixup                RadiologyOrder                report basedOn
        |                    (fhir2: ServiceRequest/<order uuid>)     |
  Orthanc -> wf         <-- ingest resolves by accession -->   poller joins on basedOn
```

Verified end to end with the tooling: loader creates patient+encounter+order for accession
`s68proof1` (priority `stat`), DICOM pushed, the orchestrator resolved it (triage scored **URGENT**
from the priority) and parked at the read gate, then `report_seeder finalize s68proof1` flipped the
seeded report to `final` and the poller released the gate.

## Tooling (scripts/mimic/, committed; MIMIC data stays off-repo per the DUA)

- `dicom_fixup.py` -- inject AccessionNumber(=study_id) + StudyDescription; keep StudyInstanceUID.
- `omrs_client.py` -- the mixed write client (fhir2 + OpenMRS REST + SQL).
- `manifest.py` + `sample_cohort.json` -- the cohort schema + a synthetic sample.
- `load_cohort.py` -- FHIR-first load (patient/encounter/order/EHR/report), then DICOM.
- `report_seeder.py` -- `finalize <accession>` rehearsal cue.
- `fetch.py` -- selective PhysioNet S3 pull (needs credentialed AWS).
- `registry_corpus.py` -- export loaded (modality, StudyDescription) for the selection test (#64).

## Known gaps / prerequisites (before a real cohort load)

1. **Order/report concept.** The demo dictionary has NO chest-x-ray concept. Provision one (a
   `bootstrap_radiology_concept.py` like the presign bootstrap) and set `MIMIC_ORDER_CONCEPT_UUID`.
   The current tooling requires this UUID and refuses to guess.
2. **EHR labs need numeric concepts.** MIMIC-IV creatinine/eGFR -> Observation needs numeric
   concepts (the demo has none) and a FHIR-valid datetime. Labs load is best-effort until concepts
   are provisioned.
3. **Meds have no create path.** fhir2 can't create MedicationRequest; OpenMRS drug orders need a
   Drug/concept model. The anticoagulant med-flag story needs this built (follow-up).
4. **reasonCode on the order (pneumothorax-detect slice).** #68 wants the order's ICD-10 reason
   (J93*/J95.811) to fire the reason-code slice, but the #70 ingest resolver currently returns only
   `priority`, not `reasonCode` (the module order reason is a Concept, not an ICD-10 code). To light
   this up, the ETL must set the order reason as an ICD-10-mapped concept AND the resolver must
   extract it. Tracked as a follow-up to the #70 resolver.
5. **DB access for the SQL order path.** The loader connects to mariadb (pymysql). Run it as a
   one-shot container on the compose network (mariadb/openmrs by service name), the way the #68
   E2E ran it -- do not publish the DB port.
6. **MIMIC-IV DUA.** A separate PhysioNet signature from MIMIC-CXR; sign early or criterion 3
   (labs/meds/problems) has no data.
