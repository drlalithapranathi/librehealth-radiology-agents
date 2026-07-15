# MIMIC-CXR showcase ETL tooling (#68)

Loads a curated ~100-study MIMIC-CXR cohort into the demo stack. Design + write-path rationale:
`docs/mimic-cxr-mapping.md`. **No MIMIC data or manifests live in this repo (PhysioNet DUA)** --
only the tooling and a synthetic `sample_cohort.json`.

## Prerequisites

- `pip install -r requirements.txt` (pydicom, boto3, httpx, pymysql).
- Credentialed PhysioNet AWS access for `fetch.py` (MIMIC-CXR **and** MIMIC-IV DUAs signed).
- The concepts the demo dictionary lacks: run `bootstrap_radiology_concept.py` (provisions the
  chest-x-ray order/report concept + numeric creatinine/eGFR lab concepts), then set
  `MIMIC_ORDER_CONCEPT_UUID` to the printed Chest-radiograph UUID.
- Run on the compose network (mariadb/openmrs reachable by service name), e.g. as a one-shot
  container; do NOT publish the DB port.

## Flow

```bash
# 0. provision the concepts the demo dictionary lacks (CXR order/report + numeric labs); prints the UUID
python bootstrap_radiology_concept.py           # -> set MIMIC_ORDER_CONCEPT_UUID to the printed value

# 1. fetch only the cohort's studies from PhysioNet S3 (off-repo dest, DUA)
python fetch.py my_cohort.json /secure/mimic-dl

# 2. FHIR first: patients, encounters, RadiologyOrders, EHR packet, seeded preliminary reports
python load_cohort.py my_cohort.json --concept $MIMIC_ORDER_CONCEPT_UUID

# 3. fix up + push DICOM (per study: accession = study_id), which starts each workflow
python dicom_fixup.py /secure/mimic-dl/.../s56699142/xxx.dcm s56699142   # then POST to Orthanc

# 4. export the loaded (modality, StudyDescription) corpus for the registry selection test (#64)
python registry_corpus.py --out registry_corpus.json

# 5. rehearsal sign-off cue (live demo: radiologists sign in the RIS instead)
python report_seeder.py finalize s56699142
```

## Proven

The join-critical path is verified end to end on the o3 stack: loader-created RadiologyOrder ->
DICOM -> orchestrator resolves it (triage URGENT from a `stat` order) -> read gate -> `finalize`
flips the seeded report to final -> poller releases the gate. See `docs/mimic-cxr-mapping.md`.

Tests (no stack, no data): `python -m pytest scripts/mimic/tests -q`.
