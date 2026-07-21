"""Bootstrap the AI authorship-stamp concepts in the OpenMRS concept
dictionary.

Two concepts, each at a stable, well-known UUID so its client-side default
(see ``libs/radagent-common/radagent_common/fhir_client.py``) can be a fixed
configuration value across deployments:

* "AI pre-sign impression draft" (``FHIR2_PRESIGN_REPORT_CONCEPT``) -- the
  ``DiagnosticReport.code`` stamp on the pre-sign draft (#26/#55).
* "AI critical result notification" (``FHIR2_CRITICAL_NOTIFICATION_CONCEPT``)
  -- the ``Observation.code`` stamp on the in-EHR critical-result
  notification the ehr-inbox channel writes (#79). Datatype **Text**,
  because that Observation carries a ``valueString`` and fhir2 refuses an
  obs whose value does not match its concept's datatype.

Run once at stack startup as a docker-compose one-shot service that depends
on the ``mariadb`` and ``openmrs`` services being healthy. (The compose
service and this file keep their original ``presign``-era names: the compose
mount and the #55 drift test reference them, and a rename would churn both
for no behavioural gain.)

Idempotent per concept: a concept already present at its target UUID is
skipped without being touched. Safe to run on every ``docker compose up``.
See ``docker/openmrs/README.md`` and ``docs/presign-concept.md`` for the
design rationale.

Why direct SQL rather than the OpenMRS REST endpoint
----------------------------------------------------
The OpenMRS ``POST /ws/rest/v1/concept`` endpoint auto-assigns UUIDs and
does not honour a caller-supplied UUID on create -- confirmed against the
webservices.rest concept resource behaviour and the OpenMRS Talk thread
"Create object via REST API with specified UUID" (2017, still current per
the module source at the pinned openmrs version). A caller-supplied UUID
is exactly what makes ``FHIR2_PRESIGN_REPORT_CONCEPT`` a stable
configuration value across deployments -- without it, every deployment
would have a different UUID and the env-var override would have to be
hand-set post-provisioning. Direct SQL insert bypasses REST and lets us
pin the UUID.

Trade-off: SQL insert bypasses OpenMRS's Hibernate-level ``ConceptService
.saveConcept`` hooks (audit event, module notifications, second-level
cache invalidation for the specific concept row). The ``creator`` and
``date_created`` columns are populated, so the row is still identifiable.
For clinical data-entry this would be unacceptable. For a deployment-time
insert of an authorship-stamp concept whose lifecycle is "created once,
never updated, never retired" it is a reasonable exchange.

Not a workaround for a bug that needs raising upstream: the REST
behaviour is by design.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Optional

import pymysql  # pure-Python; installed via `pip install pymysql` at container start


# --- Concept metadata --------------------------------------------------------
# These UUIDs are UUID5 hashes derived from a stable name in the librehealth.org
# DNS namespace; anyone can regenerate them from the same seed:
#     ns = uuid.uuid5(uuid.NAMESPACE_DNS, "librehealth.org")
#     concept       = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1")
#     concept_name  = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1.name.en")
#     concept_desc  = uuid.uuid5(ns, "lh-radiology.ai-presign-impression-draft.v1.description.en")
# Changing ANY of these three requires a corresponding update to
# libs/radagent-common/radagent_common/fhir_client.py::_DEFAULT_PRESIGN_REPORT_CONCEPT.
PRESIGN_CONCEPT_UUID = "e3641471-3f25-57b4-ab27-a3ebc66e481e"
PRESIGN_CONCEPT_NAME_UUID = "29e05193-b2ff-558c-b753-78d405211ffb"
PRESIGN_CONCEPT_DESCRIPTION_UUID = "51a62a88-c4f7-54f0-8a0f-936d2343234b"

PRESIGN_CONCEPT_NAME = "AI pre-sign impression draft"
PRESIGN_CONCEPT_DESCRIPTION = (
    "Authorship stamp for AI-generated pre-sign impression drafts written to "
    "DiagnosticReport.code by the LH-Radiology orchestrator. Not a clinical "
    "diagnosis; identifies the source of the draft so it can be safely updated "
    "on re-run without overwriting a radiologist's own preliminary draft."
)

# The critical-result notification concept (#79) derives the same way:
#     concept       = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1")
#     concept_name  = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1.name.en")
#     concept_desc  = uuid.uuid5(ns, "lh-radiology.ai-critical-result-notification.v1.description.en")
# Changing ANY of these three requires a corresponding update to
# libs/radagent-common/radagent_common/fhir_client.py::_DEFAULT_CRITICAL_NOTIFICATION_CONCEPT.
NOTIFICATION_CONCEPT_UUID = "ea215431-5e85-5040-adf0-1da297c154c3"
NOTIFICATION_CONCEPT_NAME_UUID = "ac13adf6-ff97-50bc-8d74-0e221075ad51"
NOTIFICATION_CONCEPT_DESCRIPTION_UUID = "0a55837a-b562-5b85-b313-eceafbfc90c1"

NOTIFICATION_CONCEPT_NAME = "AI critical result notification"
NOTIFICATION_CONCEPT_DESCRIPTION = (
    "Authorship stamp for the in-EHR critical-result notification Observation "
    "written by the LH-Radiology communications agent (#79). Not a clinical "
    "finding; identifies the source so a re-run updates its own notification "
    "and never touches clinician-authored data."
)

# OpenMRS reference UUIDs -- stable across every OpenMRS install (present in
# openmrs-core seed data since 2004). We look up the numeric IDs at insert time
# because concept_datatype_id and concept_class_id are auto-increment and not
# guaranteed to be stable (they usually are, but relying on the ID would be
# fragile).
DATATYPE_NA_UUID = "8d4a4c94-c2cc-11de-8d13-0010c6dffd0f"       # "N/A" -- label-only concept
DATATYPE_TEXT_UUID = "8d4a4ab4-c2cc-11de-8d13-0010c6dffd0f"     # "Text" -- carries obs value_text
CLASS_DIAGNOSIS_UUID = "8d4918b0-c2cc-11de-8d13-0010c6dffd0f"   # "Diagnosis" -- mirrors CIEL Provisional
CLASS_MISC_UUID = "8d492774-c2cc-11de-8d13-0010c6dffd0f"        # "Misc" -- a delivery artifact, not a diagnosis

# One row per provisioned concept, so adding concept N+1 is a table row -- not
# another copy of the INSERT choreography.
_CONCEPTS = [
    {
        "uuid": PRESIGN_CONCEPT_UUID,
        "name_uuid": PRESIGN_CONCEPT_NAME_UUID,
        "description_uuid": PRESIGN_CONCEPT_DESCRIPTION_UUID,
        "name": PRESIGN_CONCEPT_NAME,
        "description": PRESIGN_CONCEPT_DESCRIPTION,
        "datatype_uuid": DATATYPE_NA_UUID,
        "class_uuid": CLASS_DIAGNOSIS_UUID,
    },
    {
        "uuid": NOTIFICATION_CONCEPT_UUID,
        "name_uuid": NOTIFICATION_CONCEPT_NAME_UUID,
        "description_uuid": NOTIFICATION_CONCEPT_DESCRIPTION_UUID,
        "name": NOTIFICATION_CONCEPT_NAME,
        "description": NOTIFICATION_CONCEPT_DESCRIPTION,
        "datatype_uuid": DATATYPE_TEXT_UUID,
        "class_uuid": CLASS_MISC_UUID,
    },
]


log = logging.getLogger("bootstrap_presign_concept")


def _connect_with_retry(
    host: str, database: str, user: str, password: str,
    attempts: int = 30, delay_seconds: float = 2.0,
) -> pymysql.connections.Connection:
    """Connect to mariadb with a bounded retry loop.

    docker-compose ``depends_on: {condition: service_healthy}`` on mariadb
    means we should not normally hit connection errors -- but a slow network
    or a mariadb still finalising warmup can cause the first connect to
    fail. Retry a few times before giving up. Never blocks indefinitely.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return pymysql.connect(
                host=host,
                database=database,
                user=user,
                password=password,
                autocommit=False,
                connect_timeout=5,
            )
        except pymysql.Error as e:
            last_error = e
            log.info("mariadb not yet reachable (attempt %d/%d): %s", attempt, attempts, e)
            time.sleep(delay_seconds)
    raise RuntimeError(
        f"mariadb never became reachable at {host} after {attempts} attempts: {last_error}"
    )


def _provision_concept(cursor, spec: dict, admin_user_id: int) -> Optional[int]:
    """Provision ONE concept from its `_CONCEPTS` row. Returns its concept_id,
    or None when the OpenMRS reference seed data (datatype/class) is missing.
    Idempotent: a concept already present at the target UUID is left untouched.
    """
    cursor.execute(
        "SELECT concept_id FROM concept WHERE uuid = %s", (spec["uuid"],),
    )
    row = cursor.fetchone()
    if row is not None:
        concept_id = row[0]
        log.info(
            "Concept %s already exists (concept_id=%d), skipping insert.",
            spec["uuid"], concept_id,
        )
        return concept_id

    # Resolve the reference datatype UUID -> ID.
    cursor.execute(
        "SELECT concept_datatype_id FROM concept_datatype WHERE uuid = %s",
        (spec["datatype_uuid"],),
    )
    row = cursor.fetchone()
    if row is None:
        log.error(
            "Reference concept datatype (UUID %s) not found. The OpenMRS core "
            "seed data appears not to be loaded. Bring up the openmrs service first "
            "and wait for its healthcheck to pass before running this bootstrap.",
            spec["datatype_uuid"],
        )
        return None
    datatype_id = row[0]

    # Resolve the reference class UUID -> ID.
    cursor.execute(
        "SELECT concept_class_id FROM concept_class WHERE uuid = %s",
        (spec["class_uuid"],),
    )
    row = cursor.fetchone()
    if row is None:
        log.error(
            "Reference concept class (UUID %s) not found. Same likely cause "
            "as a datatype miss.",
            spec["class_uuid"],
        )
        return None
    class_id = row[0]

    # Insert the concept row.
    cursor.execute(
        """
        INSERT INTO concept
            (retired, datatype_id, class_id, is_set, creator, date_created, uuid)
        VALUES (0, %s, %s, 0, %s, NOW(), %s)
        """,
        (datatype_id, class_id, admin_user_id, spec["uuid"]),
    )
    concept_id = cursor.lastrowid

    # Insert the fully-specified English name.
    cursor.execute(
        """
        INSERT INTO concept_name
            (concept_id, name, locale, locale_preferred, creator, date_created,
             concept_name_type, voided, uuid)
        VALUES (%s, %s, 'en', 1, %s, NOW(), 'FULLY_SPECIFIED', 0, %s)
        """,
        (concept_id, spec["name"], admin_user_id, spec["name_uuid"]),
    )

    # Insert the English description.
    cursor.execute(
        """
        INSERT INTO concept_description
            (concept_id, description, locale, creator, date_created, uuid)
        VALUES (%s, %s, 'en', %s, NOW(), %s)
        """,
        (
            concept_id, spec["description"],
            admin_user_id, spec["description_uuid"],
        ),
    )

    log.info(
        "Provisioned concept %s (concept_id=%d, name=%r).",
        spec["uuid"], concept_id, spec["name"],
    )
    return concept_id


def bootstrap(
    host: str, database: str, user: str, password: str,
) -> int:
    """Return exit code. 0 = success (idempotent no-op or created), non-zero = failure."""
    conn = _connect_with_retry(host=host, database=database, user=user, password=password)
    try:
        with conn.cursor() as cursor:
            # Get the admin user id for the audit columns. Fall back to user_id=1 (the OpenMRS
            # default admin), which every seeded install has.
            cursor.execute("SELECT user_id FROM users WHERE system_id = %s LIMIT 1", ("admin",))
            row = cursor.fetchone()
            admin_user_id = row[0] if row is not None else 1

            for spec in _CONCEPTS:
                if _provision_concept(cursor, spec, admin_user_id) is None:
                    conn.rollback()
                    return 2

            conn.commit()
            return 0
    except pymysql.Error as e:
        conn.rollback()
        log.exception("Bootstrap failed with a mariadb error: %s", e)
        return 3
    finally:
        conn.close()


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("OMRS_DB_HOSTNAME", "mariadb"))
    parser.add_argument("--database", default=os.environ.get("OMRS_DB_NAME", "openmrs"))
    parser.add_argument("--user", default=os.environ.get("OMRS_DB_USERNAME", "openmrs"))
    parser.add_argument("--password", default=os.environ.get("OMRS_DB_PASSWORD", "openmrs"))
    return parser.parse_args(argv)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    args = _parse_args()
    return bootstrap(
        host=args.host, database=args.database, user=args.user, password=args.password,
    )


if __name__ == "__main__":
    sys.exit(main())
