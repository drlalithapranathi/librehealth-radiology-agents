"""Bootstrap the "AI pre-sign impression draft" concept in the OpenMRS
concept dictionary.

Ensures the concept exists at a stable, well-known UUID so that
``FHIR2_PRESIGN_REPORT_CONCEPT`` (see
``libs/radagent-common/radagent_common/fhir_client.py``) can be a fixed
configuration value across deployments. Run once at stack startup as a
docker-compose one-shot service that depends on the ``mariadb`` and
``openmrs`` services being healthy.

Idempotent: if the concept already exists at the target UUID, the script
exits 0 without touching anything. Safe to run on every ``docker compose
up``. See ``docker/openmrs/README.md`` and ``docs/presign-concept.md`` for
the design rationale.

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

# OpenMRS reference UUIDs -- stable across every OpenMRS install (present in
# openmrs-core seed data since 2004). We look up the numeric IDs at insert time
# because concept_datatype_id and concept_class_id are auto-increment and not
# guaranteed to be 4 and 4 respectively (they usually are, but relying on the
# ID would be fragile).
DATATYPE_NA_UUID = "8d4a4c94-c2cc-11de-8d13-0010c6dffd0f"       # "N/A" -- label-only concept
CLASS_DIAGNOSIS_UUID = "8d4918b0-c2cc-11de-8d13-0010c6dffd0f"   # "Diagnosis" -- mirrors CIEL Provisional


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


def bootstrap(
    host: str, database: str, user: str, password: str,
) -> int:
    """Return exit code. 0 = success (idempotent no-op or created), non-zero = failure."""
    conn = _connect_with_retry(host=host, database=database, user=user, password=password)
    try:
        with conn.cursor() as cursor:
            # Idempotency guard: is the concept already there?
            cursor.execute(
                "SELECT concept_id FROM concept WHERE uuid = %s", (PRESIGN_CONCEPT_UUID,),
            )
            row = cursor.fetchone()
            if row is not None:
                concept_id = row[0]
                log.info(
                    "Concept %s already exists (concept_id=%d), skipping insert.",
                    PRESIGN_CONCEPT_UUID, concept_id,
                )
                return 0

            # Resolve the reference datatype UUID -> ID.
            cursor.execute(
                "SELECT concept_datatype_id FROM concept_datatype WHERE uuid = %s",
                (DATATYPE_NA_UUID,),
            )
            row = cursor.fetchone()
            if row is None:
                log.error(
                    "Reference concept datatype 'N/A' (UUID %s) not found. The OpenMRS core "
                    "seed data appears not to be loaded. Bring up the openmrs service first "
                    "and wait for its healthcheck to pass before running this bootstrap.",
                    DATATYPE_NA_UUID,
                )
                return 2
            datatype_id = row[0]

            # Resolve the reference class UUID -> ID.
            cursor.execute(
                "SELECT concept_class_id FROM concept_class WHERE uuid = %s",
                (CLASS_DIAGNOSIS_UUID,),
            )
            row = cursor.fetchone()
            if row is None:
                log.error(
                    "Reference concept class 'Diagnosis' (UUID %s) not found. Same likely cause "
                    "as the datatype miss above.",
                    CLASS_DIAGNOSIS_UUID,
                )
                return 2
            class_id = row[0]

            # Get the admin user id for the audit columns. Fall back to user_id=1 (the OpenMRS
            # default admin), which every seeded install has.
            cursor.execute("SELECT user_id FROM users WHERE system_id = %s LIMIT 1", ("admin",))
            row = cursor.fetchone()
            admin_user_id = row[0] if row is not None else 1

            # Insert the concept row.
            cursor.execute(
                """
                INSERT INTO concept
                    (retired, datatype_id, class_id, is_set, creator, date_created, uuid)
                VALUES (0, %s, %s, 0, %s, NOW(), %s)
                """,
                (datatype_id, class_id, admin_user_id, PRESIGN_CONCEPT_UUID),
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
                (concept_id, PRESIGN_CONCEPT_NAME, admin_user_id, PRESIGN_CONCEPT_NAME_UUID),
            )

            # Insert the English description.
            cursor.execute(
                """
                INSERT INTO concept_description
                    (concept_id, description, locale, creator, date_created, uuid)
                VALUES (%s, %s, 'en', %s, NOW(), %s)
                """,
                (
                    concept_id, PRESIGN_CONCEPT_DESCRIPTION,
                    admin_user_id, PRESIGN_CONCEPT_DESCRIPTION_UUID,
                ),
            )

            conn.commit()
            log.info(
                "Provisioned concept %s (concept_id=%d, name=%r).",
                PRESIGN_CONCEPT_UUID, concept_id, PRESIGN_CONCEPT_NAME,
            )
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
