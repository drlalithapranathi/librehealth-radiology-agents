# EMBED mammography -> LH-Radiology agent workflow (mapping spike)

Owner: Saptarshi (PI). Issue: #31 (`type::spike`). Status: **design hardened, build is a follow-up.**

This is the hardened version of the first-cut sketch in issue #31. A follow-up
implementation MR executes against this document. Nothing here is milestoned into M1;
the ETL/showcase build leans M2 (post real fhir2 reads). See [Open questions](#open-questions).

Honors [Golden rule 2](../CLAUDE.md): A2A messages stay lean-reference. Agents read
clinical data from `fhir2` and imaging metadata from Orthanc. The ETL below populates
those two stores; it does not push clinical payloads through the agents.

## 1. What EMBED gives us, and what it does not

[EMBED](https://github.com/Emory-HITI/EMBED_Open_Data) (Emory Breast Imaging Dataset) ships
real screening/diagnostic mammography DICOM, structured MagView descriptors, free-text
pathology, and demographics with ground-truth BI-RADS and pathology labels. That is a strong
fit for the **Triage -> Impression -> Verification** path.

It does **not** ship (a) native FHIR/EHR resources or (b) RIS report/sign-off artifacts. So the
core of the build is an **EMBED CSV -> FHIR R4 / OpenMRS (fhir2)** mapping plus a curation/ETL
path, and seeding DiagnosticReports so the RIS poller has a `preliminary -> final` transition to
detect.

Access is controlled (academic, non-commercial, no redistribution, Google-Form application). It
is fine as a flagship demo on provisioned hardware but **cannot** be the default
reproducible-for-contributors dataset. Track a fully-open fallback (CBIS-DDSM / RSNA-Kaggle)
separately.

## 2. EMBED tables and confirmed column names

Two CSVs, joined on anonymized patient + accession IDs:

- `EMBED_OpenData_metadata.csv` -- image-level, one row per DICOM file.
- `EMBED_OpenData_clinical.csv` -- exam/finding-level, one row per finding (MagView descriptors + outcomes).

Confirmed against the public data descriptor (2026-07-08):

| Column | Confirmed | Notes |
|---|---|---|
| `empi_anon`, `acc_anon`, `study_date_anon` | yes | join keys + exam date |
| `desc` | yes | procedure description |
| `asses` | yes | BI-RADS assessment letter (see 5.1) |
| `tissueden` | yes | breast density 1-4 (see 5.2) |
| `side`, `numfind` | yes | finding laterality; finding count |
| `massshape`, `massmargin`, `massdens` | yes | mass descriptors |
| `calcfind`, `calcdistri` | yes | calcification descriptors |
| `path_severity` | yes | pathology severity (0=invasive ... 6; null=not biopsied) |
| `RACE_DESC`, `FinalImageType`, `ViewPosition`, `ImageLateralityFinal` | yes | demographics + image metadata |
| `ETHNIC_GROUP_DESC` | corrected | sketch said `ETHNICITY_DESC`; descriptor uses `ETHNIC_GROUP_DESC` |
| `bside` | **confirm at ingest** | descriptor unclear; likely pathology-result laterality |
| `calcnumber` | **confirm at ingest** | not found in descriptor; may be folded into `calcfind` |
| `GENDER_DESC`, `age_at_study` | **confirm at ingest** | not enumerated in the descriptor view; present in most EMBED releases, verify the exact header |

Action for the build MR: diff these against the actual CSV header of the provisioned release
before writing the ETL, and update this table.

## 3. Identifiers (join keys)

| EMBED field | Role | FHIR target |
|---|---|---|
| `empi_anon` | anonymized patient | `Patient.identifier` (system `embed/empi`) |
| `acc_anon` | anonymized accession/exam | `ServiceRequest.identifier` + `ImagingStudy.identifier` (accession) + DICOM `AccessionNumber` |
| DICOM `StudyInstanceUID` | imaging study | `ImagingStudy.identifier` -> Orthanc study id -> `wf_<orthancStudyId>` |

The DICOM `AccessionNumber` is the pivot: it ties the Orthanc study (and therefore
`wf_<orthancStudyId>`) back to the EMBED-derived `ServiceRequest`/`DiagnosticReport` in fhir2.

## 4. EMBED -> FHIR R4 resources

| Source (EMBED) | FHIR R4 | Codes / notes |
|---|---|---|
| Demographics (`GENDER_DESC`, `age_at_study`, `RACE_DESC`, `ETHNIC_GROUP_DESC`) | **Patient** | gender; derive approximate `birthDate` from age@study and study date; US-Core race/ethnicity extensions |
| `desc`, `study_date_anon`, `acc_anon` | **ServiceRequest** | `code` = mammography (LOINC 24606-6 screening / 24610-8 diagnostic; or CPT 77067/77066); `subject`->Patient; `occurrence`=study date; status `completed` |
| metadata table (views CC/MLO from `ViewPosition`, laterality `ImageLateralityFinal`, `FinalImageType` 2D/cview/tomo) | **ImagingStudy** | `modality`=MG; series/instances from Orthanc; `endpoint`->Orthanc WADO-RS; `basedOn`->ServiceRequest |
| the read + `asses` + free text | **DiagnosticReport** (imaging) | `code` LOINC 24606-6; `status` seed `preliminary` then flip to `final` (exercises the RIS poller); `conclusionCode`=BI-RADS; `imagingStudy`->ImagingStudy; `result`->Observations below |
| `asses` | **Observation** (BI-RADS assessment) | coded; drives Triage priority + Verification concordance (see 5.1) |
| `tissueden` | **Observation** (breast composition) | LOINC 72134-0 (see 5.2) |
| `side` / `bside` | Observation component / `bodySite` | laterality; a Verification rule input |
| mass (`massshape`, `massmargin`, `massdens`), calcifications (`calcfind`, `calcdistri`), `numfind` | **Observation** components (per finding) | lesion descriptors |
| `path_severity` + free-text pathology | **DiagnosticReport** (pathology) + **Condition** | ground truth for Impression/Verification; `conclusionCode` malignant/benign |
| the visit | **Encounter** | ties Patient + ServiceRequest + DiagnosticReport |

## 5. Code bindings

### 5.1 BI-RADS from `asses` (confirmed against descriptor)

| `asses` | BI-RADS | Meaning | Actionable? |
|---|---|---|---|
| A | 0 | Additional evaluation needed | **yes** (recall) |
| N | 1 | Negative | no |
| B | 2 | Benign | no |
| P | 3 | Probably benign | short-interval follow-up |
| S | 4 | Suspicious | **yes** (biopsy) |
| M | 5 | Highly suggestive of malignancy | **yes** (biopsy) |
| K | 6 | Known biopsy-proven malignancy | already known |

Triage: BI-RADS 0/4/5 -> high priority. Communications: dispatch on 0/4/5 (recall/suspicious).

### 5.2 Breast density from `tissueden`

`tissueden` 1-4 -> ACR density A/B/C/D, Observation LOINC 72134-0.

### 5.3 Laterality

EMBED `side` / `ImageLateralityFinal` L/R/B -> Observation `bodySite` laterality. Consumed by the
existing `laterality-consistency` verification rule.

## 6. Curation slice (demo-sized)

Only ~20% of 2D/C-view is released today and the full set is multi-TB, so curate a demo slice
spread across the BI-RADS spectrum:

- BI-RADS 1 (negative) x a few normals
- BI-RADS 2 (benign)
- BI-RADS 0 (recall) -> exercises Communications recall path
- BI-RADS 4/5 (suspicious/malignant) with `path_severity` non-null -> exercises Impression +
  Verification against ground truth, and the pathology `DiagnosticReport`/`Condition`
- a small L/R/bilateral mix so laterality rules have something to check

Target size: a few dozen studies, small enough for a single demo box's Orthanc. Exact accession
list is picked once the provisioned release is in hand.

## 7. ETL shape (design only; build is a follow-up)

Two independent loads keyed on `AccessionNumber`:

1. **DICOM -> Orthanc.** Push the curated slice via C-STORE / Orthanc REST. `OnStableStudy`
   fires the ingress webhook (existing plugin), which starts `wf_<orthancStudyId>`.
2. **CSV -> FHIR bundles -> OpenMRS via fhir2.** Transform clinical + metadata rows into the
   Patient / ServiceRequest / ImagingStudy / DiagnosticReport / Observation / Condition / Encounter
   bundle above; POST to fhir2. Seed the imaging `DiagnosticReport` at `preliminary`, then flip to
   `final` on cue so the RIS poller detects `report_finalized` and exercises the human-gated
   sign-off loop.

Ordering: load FHIR first (so patient/order resolve), then push DICOM (so the workflow starts
with EHR context resolvable). De-identification is already done by EMBED; the ETL adds no PHI.

## 8. Verification rule seed (owned here)

Seeded from EMBED BI-RADS semantics under `agents/report-verification/rules/`. Following the
existing convention (e.g. `laterality-consistency.yaml`), these reference the M2 report-body
fields and are **harmless no-ops until report-body parsing lands**; the modality-gated checks live
in `rules/custom/` so they never fire on a non-mammography study.

| Rule | Kind | Fires when |
|---|---|---|
| `mammo-birads-code-valid.yaml` | YAML | `report.body.biradsAssessment` is out of the 0-6 range |
| `mammo_actionable_needs_followup.py` | custom | BI-RADS 0/4/5 present but no recommendation recorded |
| `mammo_density_stated.py` | custom | a mammography read carries a BI-RADS assessment but no breast-density statement |

Field convention these assume (to be produced by the ETL / M2 report-body parse):
`report.body.biradsAssessment` (int 0-6), `report.body.breastDensity` ("A".."D"),
`report.body.laterality` ("L"/"R"/"B").

## 9. Data-access application checklist

EMBED access is a Google-Form application with lead time. This needs a human (PI) to submit; it
cannot be automated.

- [ ] Read the [EMBED license](https://github.com/Emory-HITI/EMBED_Open_Data/blob/main/EMBED_license.md).
- [ ] Submit the access application (institution + non-commercial use statement).
- [ ] Confirm provisioned-hardware storage for the curated slice.
- [ ] Once granted: diff the real CSV header against section 2 and update.

## Open questions

- **Milestone.** ETL/showcase leans M2 (post real fhir2 reads); this mapping design can proceed
  now unmilestoned. Do not move #31 into M1.
- **Longitudinal priors.** Synthesize priors for the EHR Assistant, or scope v1 to single-exam
  demographics? Recommend single-exam for v1, flag synthesized-vs-empty in the EHR context.
- **ROI representation.** DICOM SR vs. sidecar annotation for the Interpretation/OHIF overlay.
