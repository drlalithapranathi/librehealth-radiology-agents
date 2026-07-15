"""Provision the concepts the MIMIC ETL needs but the demo dictionary lacks (#68).

The o3 demo dictionary has NO chest-x-ray procedure and NO numeric lab concepts, so an order/report
cannot be coded and labs cannot be stored. This bootstrap inserts, at STABLE UUID5s (so config can
reference fixed values across deployments):
  - "Chest radiograph"  -> the order + DiagnosticReport concept (set MIMIC_ORDER_CONCEPT_UUID to it)
  - "Serum creatinine", "Estimated GFR" -> Numeric lab concepts for the EHR packet (creatinine/eGFR)

Direct SQL, mirroring docker/openmrs/bootstrap_presign_concept.py: the OpenMRS REST concept endpoint
auto-assigns UUIDs and will not honour a caller-supplied one, and a stable UUID is exactly what makes
MIMIC_ORDER_CONCEPT_UUID a fixed config value. Idempotent: a concept already present at its UUID is
left untouched. Run once before load_cohort (as a one-shot container on the compose network).

Loader coupling: load_cohort imports LAB_LOINC_TO_CONCEPT from here to map the manifest's LOINC lab
codes onto these concept UUIDs.
"""
from __future__ import annotations
import argparse
import os
import sys
import uuid

# pymysql is imported lazily in main() so load_cohort can import the concept UUIDs / LOINC map
# below without pulling in the DB driver.

# --- stable UUID5s (regenerable from the same seeds) -------------------------
_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "librehealth.org")


def _u(seed: str) -> str:
    return str(uuid.uuid5(_NS, seed))


CHEST_RADIOGRAPH_UUID = _u("lh-radiology.mimic.chest-radiograph.v1")
CREATININE_UUID = _u("lh-radiology.mimic.serum-creatinine.v1")
EGFR_UUID = _u("lh-radiology.mimic.egfr.v1")

# LOINC -> provisioned concept, for the loader's EHR labs.
LAB_LOINC_TO_CONCEPT = {
    "2160-0": CREATININE_UUID,                 # Creatinine [Mass/volume] in Serum or Plasma
    "33914-3": EGFR_UUID, "48642-3": EGFR_UUID, "62238-1": EGFR_UUID,  # eGFR variants
}

# OpenMRS reference rows (present in every seeded install; verified on the o3 stack).
NA_DATATYPE = "8d4a4c94-c2cc-11de-8d13-0010c6dffd0f"
NUMERIC_DATATYPE = "8d4a4488-c2cc-11de-8d13-0010c6dffd0f"
RADIOLOGY_CLASS = "8caa332c-efe4-4025-8b18-3398328e1323"   # Radiology/Imaging Procedure
TEST_CLASS = "8d4907b2-c2cc-11de-8d13-0010c6dffd0f"        # Test

CONCEPTS = [
    {"uuid": CHEST_RADIOGRAPH_UUID, "name": "Chest radiograph",
     "class": RADIOLOGY_CLASS, "datatype": NA_DATATYPE, "numeric": None},
    {"uuid": CREATININE_UUID, "name": "Serum creatinine",
     "class": TEST_CLASS, "datatype": NUMERIC_DATATYPE, "numeric": {"units": "mg/dL"}},
    {"uuid": EGFR_UUID, "name": "Estimated GFR",
     "class": TEST_CLASS, "datatype": NUMERIC_DATATYPE, "numeric": {"units": "mL/min/1.73m2"}},
]


def _ref_id(cur, table: str, id_col: str, uuid_val: str) -> int:
    cur.execute(f"select {id_col} from {table} where uuid=%s", (uuid_val,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"reference {table} {uuid_val} not found -- is the OpenMRS seed loaded?")
    return row[0]


def provision(conn, spec: dict) -> str:
    with conn.cursor() as cur:
        cur.execute("select concept_id from concept where uuid=%s", (spec["uuid"],))
        if cur.fetchone():
            return "exists"
        datatype_id = _ref_id(cur, "concept_datatype", "concept_datatype_id", spec["datatype"])
        class_id = _ref_id(cur, "concept_class", "concept_class_id", spec["class"])
        cur.execute("select user_id from users where system_id='admin' limit 1")
        admin = (cur.fetchone() or [1])[0]
        cur.execute(
            "insert into concept (retired, datatype_id, class_id, is_set, creator, date_created, uuid) "
            "values (0, %s, %s, 0, %s, NOW(), %s)", (datatype_id, class_id, admin, spec["uuid"]))
        cid = cur.lastrowid
        cur.execute(
            "insert into concept_name (concept_id, name, locale, locale_preferred, creator, "
            "date_created, concept_name_type, voided, uuid) "
            "values (%s, %s, 'en', 1, %s, NOW(), 'FULLY_SPECIFIED', 0, %s)",
            (cid, spec["name"], admin, _u(spec["uuid"] + ".name.en")))
        if spec["numeric"] is not None:
            # allow_decimal=1: lab values like creatinine 0.9 are decimals; fhir2 rejects a decimal
            # obs against a numeric concept whose allow_decimal is false (the column default).
            cur.execute("insert into concept_numeric (concept_id, units, allow_decimal) "
                        "values (%s, %s, 1)", (cid, spec["numeric"]["units"]))
        return "created"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Provision MIMIC ETL concepts (#68).")
    p.add_argument("--host", default=os.environ.get("OMRS_DB_HOST", "mariadb"))
    p.add_argument("--database", default=os.environ.get("OMRS_DB_NAME", "openmrs"))
    p.add_argument("--user", default=os.environ.get("OMRS_DB_USER", "openmrs"))
    p.add_argument("--password", default=os.environ.get("OMRS_DB_PASS", "openmrs"))
    args = p.parse_args(argv)
    import pymysql
    conn = pymysql.connect(host=args.host, database=args.database, user=args.user,
                           password=args.password, autocommit=False)
    try:
        for spec in CONCEPTS:
            status = provision(conn, spec)
            print(f"{spec['name']:20} {spec['uuid']}  [{status}]")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"\nset MIMIC_ORDER_CONCEPT_UUID={CHEST_RADIOGRAPH_UUID}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
