# docker/openmrs/

Deployment-time bootstrap for OpenMRS metadata that the LH-Radiology
orchestrator relies on but that the o3 image itself does not ship.

## `bootstrap_presign_concept.py`

Idempotent one-shot script that ensures the **AI pre-sign impression
draft** concept exists in the OpenMRS concept dictionary at a stable,
well-known UUID (`e3641471-3f25-57b4-ab27-a3ebc66e481e`). This concept
is what `write_presign_impression` (issue #26) puts on
`DiagnosticReport.code.coding[0].code` to authorship-stamp the AI's draft.
Without it, the pre-sign write path 500s with a `codeRequired` error, and
`_find_presign_draft`'s discriminator has nothing to match against.

For the "why a dedicated concept and not the CIEL Provisional Diagnosis"
rationale, see [`docs/presign-concept.md`](../../docs/presign-concept.md).

### How the dev stack runs it

`docker-compose.yml` defines a `presign-concept-bootstrap` service that:

- depends on `openmrs` and `mariadb` being healthy,
- installs `pymysql` and runs this script,
- exits 0 either after inserting the concept or after finding it already
  present.

It runs on every `docker compose up`; the idempotency check makes
subsequent runs no-ops.

### How real deployments should run it

Any of the following works; pick the one that fits your operations
model:

1. **Reuse the compose service.** Copy the `presign-concept-bootstrap`
   block from `docker-compose.yml` (and the associated
   `docker/openmrs/bootstrap_presign_concept.py` file) into the target
   deployment's compose stack. Set the four `OMRS_DB_*` env vars to
   point at the deployment's mariadb. Requires network access from a
   sidecar to the mariadb instance.

2. **Run this script directly against production mariadb.** Once, from a
   host that can reach the DB:
   ```
   pip install pymysql
   python bootstrap_presign_concept.py \
       --host <mariadb-host> --database <db> --user <user> --password <pw>
   ```
   Same idempotency guarantee.

3. **Bake into the o3 module's metadata bundle.** The concept can be
   added to the LibreHealth Radiology o3 module's Liquibase changesets
   or metadatadeploy resources. This is the cleanest long-term home and
   the bootstrap script becomes unnecessary for deployments that use
   that module version. Not in scope for this MR (it would require
   changes to the sibling `lh-radiology` repository).

### Why direct SQL and not the OpenMRS REST endpoint

The OpenMRS `POST /ws/rest/v1/concept` endpoint auto-assigns UUIDs and
does not honour a caller-supplied UUID. A caller-supplied UUID is what
makes `FHIR2_PRESIGN_REPORT_CONCEPT` a stable configuration value across
deployments -- without it, every deployment would end up with a
different UUID and the env-var override would have to be hand-set
post-provisioning.

Direct SQL insert against `concept`, `concept_name`, and
`concept_description` bypasses REST and lets us pin the UUID. The
trade-off is we skip OpenMRS's Hibernate-level audit hooks and
second-level cache invalidation. For a deployment-time provisioning of
an authorship-stamp concept whose lifecycle is "created once, never
updated, never retired," that is a reasonable exchange. See the module
docstring in `bootstrap_presign_concept.py` for the full rationale.
