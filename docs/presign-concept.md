# The AI pre-sign impression draft concept

This document exists because the pre-sign impression draft (issue #26)
requires a dedicated OpenMRS Concept as its authorship stamp, and that
concept has to be provisioned somewhere. This is the "somewhere," plus
the reasoning behind picking one over the alternatives.

## What the concept is

The pre-sign impression draft is a `preliminary` DiagnosticReport that
the orchestrator writes to the RIS when the Interpretation Assistant's
tools return findings tagged `COMPLETE` (M3 gate). It exists as an
advisory to the radiologist -- never signed, never transitioned to
`final`; the radiologist's own signed report is a separate `final`
DiagnosticReport the RIS creates on sign-off.

The AI's draft is distinguished from the radiologist's own preliminary
draft by the `code` concept on the DiagnosticReport: our draft carries
this dedicated concept, a radiologist's own draft carries whatever the
RIS assigns (or nothing). `_find_presign_draft` in
[`fhir_client.py`](../libs/radagent-common/radagent_common/fhir_client.py)
uses that concept as its discriminator when deciding whether an incoming
DiagnosticReport is safe to update on the pre-sign re-run path. Without
a dedicated concept, the discriminator could match a human's draft and
the update would silently overwrite the radiologist's text with the AI's
-- exactly the bug the concept stamp exists to prevent.

## Identity

- **UUID**: `e3641471-3f25-57b4-ab27-a3ebc66e481e`
- **Fully specified name (en)**: "AI pre-sign impression draft"
- **Concept class**: Diagnosis (`8d4918b0-c2cc-11de-8d13-0010c6dffd0f`)
- **Concept datatype**: N/A (`8d4a4c94-c2cc-11de-8d13-0010c6dffd0f`) --
  label-only; the concept doesn't store a value.
- **Description**: "Authorship stamp for AI-generated pre-sign
  impression drafts written to DiagnosticReport.code by the LH-Radiology
  orchestrator. Not a clinical diagnosis; identifies the source of the
  draft so it can be safely updated on re-run without overwriting a
  radiologist's own preliminary draft."

The UUID is deterministic: it is `uuid5(uuid5(NAMESPACE_DNS,
"librehealth.org"), "lh-radiology.ai-presign-impression-draft.v1")`.
Anyone can regenerate the same UUID from the same seed. This is
intentional so that the value in
`libs/radagent-common/radagent_common/fhir_client.py` isn't a "magic
number" -- it's traceable back to a documented derivation.

## Why not the CIEL "Provisional diagnosis" concept

The pre-sign write path shipped in #26 defaulted to CIEL "Provisional
diagnosis" (`160249AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA`) as a stand-in. Two
reasons that was only ever a stand-in and had to be replaced before M3
turns on the pre-sign write:

1. **Authorship-collision risk.** The `_find_presign_draft` guard tells
   our AI drafts from a radiologist's own drafts by matching on the
   `code` concept. If a deployment's RIS also drafts its own
   preliminary DiagnosticReports coded with "Provisional diagnosis" --
   which some RIS workflows do -- the concept stops being a
   discriminator. Our idempotent-update path would then PUT over the
   radiologist's draft, silently replacing human text with AI text.
   Using a concept nobody else uses removes that risk entirely.
2. **Semantics.** Coding an AI-drafted impression as "Provisional
   diagnosis" implies the AI made a diagnosis. It didn't; it produced
   an advisory draft. A concept named "AI pre-sign impression draft"
   is honest about what the resource actually is.

## Where the concept must exist

The concept must be present in the concept dictionary of the OpenMRS
instance the orchestrator writes to. If it isn't, live fhir2 rejects
`POST /DiagnosticReport` with a `codeRequired` 500 error at the point
where the AI's first `COMPLETE` finding tries to become a draft. The
`_find_presign_draft` lookup would also skip every candidate, so
re-runs would accumulate new drafts rather than updating the existing
one.

## Provisioning: three options

### Option 1: The dev-stack bootstrap service (default)

The dev stack's `docker-compose.yml` includes a
`presign-concept-bootstrap` one-shot service. On every `docker compose
up` it runs
[`docker/openmrs/bootstrap_presign_concept.py`](../docker/openmrs/bootstrap_presign_concept.py),
which does the direct SQL insert into `concept`, `concept_name`, and
`concept_description` (bypassing REST because the REST endpoint
auto-assigns UUIDs and won't honour ours). The script is idempotent --
the second and subsequent runs find the concept and exit 0 without
touching anything.

Concretely, the dev stack works out of the box: the concept ends up in
mariadb, the orchestrator's `FHIR2_PRESIGN_REPORT_CONCEPT` env var is
set to the same UUID in `docker-compose.yml`, and pre-sign writes to
fhir2 resolve to a real concept.

### Option 2: Run the same script against a real deployment's mariadb

For a non-compose production deployment (say, an existing OpenMRS
running against a managed database), copy
`docker/openmrs/bootstrap_presign_concept.py` to a host that can reach
the mariadb and run:

```
pip install pymysql==1.1.0
python bootstrap_presign_concept.py \
    --host <mariadb-host> --database <db> --user <user> --password <pw>
```

The script honours the same env-var fallbacks (`OMRS_DB_HOSTNAME` etc.)
if you'd rather not pass them on the command line. Same idempotency
guarantee. Set `FHIR2_PRESIGN_REPORT_CONCEPT` on the orchestrator to
the same UUID (`e3641471-3f25-57b4-ab27-a3ebc66e481e`) and the pre-sign
write path works.

### Option 3: Bake into the LibreHealth Radiology o3 module (long-term home)

Both options above are runtime provisioning. The cleanest long-term
home for a domain concept of the LH-Radiology solution is the o3
module's metadata bundle -- Liquibase changesets or a metadatadeploy
resource in the sibling `lh-radiology` repository. That version of the
module would ship the concept as part of module install, and the
bootstrap service becomes unnecessary for deployments that use it.
This is out of scope for this MR (it would touch a different
repository) but is the intended eventual state.

## Overriding the UUID for a deployment

`FHIR2_PRESIGN_REPORT_CONCEPT` on any service that runs
`write_presign_impression` (currently only `orchestrator`) overrides
the in-code default. A deployment that provisions a different concept
-- for example, one bundled by a modified LH-Radiology o3 module, or
one imported from OCL -- sets that env var to the concept's UUID and
skips the bootstrap script.

If the env var is set but the concept doesn't exist in the concept
dictionary, the pre-sign write 500s at first attempt. That failure is
loud (visible in the orchestrator logs and in Jaeger if OTel is on),
which is preferable to silently falling back to the default and
introducing an authorship collision.

## Changing the UUID

Both places need to change together:

1. `libs/radagent-common/radagent_common/fhir_client.py::
   _DEFAULT_PRESIGN_REPORT_CONCEPT`
2. `docker-compose.yml`'s `FHIR2_PRESIGN_REPORT_CONCEPT` env var on the
   orchestrator service
3. The three `PRESIGN_CONCEPT_*_UUID` constants at the top of
   `docker/openmrs/bootstrap_presign_concept.py`

The test at
`libs/radagent-common/tests/test_fhir_client.py::_OUR_CONCEPT` will
fail if only the constant is changed and the assertion isn't -- that's
the guard rail.

Do not rotate the UUID without a specific reason. Once a real
deployment has drafts stamped with the old UUID, changing it means
`_find_presign_draft` no longer matches those old drafts, and the
first re-run POSTs a new draft rather than updating the old one --
resulting in duplicate drafts on the same order. Retiring the old
concept and migrating existing drafts to the new UUID is a
deployment-level concern, not something the code path handles.
