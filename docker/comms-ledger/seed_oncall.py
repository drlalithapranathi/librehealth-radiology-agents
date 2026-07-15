"""Seed the on-call directory into the comms-ledger HAPI (#69).

One general-radiology Practitioner + an active PractitionerRole tagged with the on-call code
(radagent_common.comms_ledger.ON_CALL_CODE matches `code.coding[].code == "on-call"` client-side).
A single rota deliberately sidesteps the specialty-routing gap (#58): `resolve_on_call_provider`
searches with no specialty, so one active on-call role is sufficient and unambiguous.

IDEMPOTENT: both resources are PUT at KNOWN ids, so every `docker compose up` converges to the
same directory state instead of accumulating duplicates -- same one-shot contract as
presign-concept-bootstrap. HAPI treats PUT-at-id as create-or-replace.

Runs inside the compose network against COMMS_LEDGER_BASE_URL (default matches the compose
service). No PHI: the on-call directory is staff data, not patient data.
"""
from __future__ import annotations

import os
import sys

import httpx

BASE = os.environ.get("COMMS_LEDGER_BASE_URL", "http://comms-ledger:8080/fhir").rstrip("/")

PRACTITIONER = {
    "resourceType": "Practitioner",
    "id": "oncall-general-radiologist",
    "active": True,
    "name": [{"family": "On-Call", "given": ["General", "Radiology"]}],
}

ROLE = {
    "resourceType": "PractitionerRole",
    "id": "oncall-general-radiology",
    "active": True,
    "practitioner": {"reference": "Practitioner/oncall-general-radiologist"},
    # ON_CALL_CODE: search_on_call_roles pushes active=true to the server and matches this code
    # client-side -- this coding is what makes the role "reachable right now".
    "code": [{"coding": [{"system": "http://critcom/role", "code": "on-call",
                          "display": "On call"}]}],
}


def put(client: httpx.Client, resource: dict) -> None:
    url = f"{BASE}/{resource['resourceType']}/{resource['id']}"
    r = client.put(url, json=resource, headers={"Content-Type": "application/fhir+json"})
    r.raise_for_status()
    print(f"seeded {resource['resourceType']}/{resource['id']} "
          f"({'created' if r.status_code == 201 else 'updated'})")


def main() -> int:
    with httpx.Client(timeout=30) as client:
        put(client, PRACTITIONER)
        put(client, ROLE)
    print("on-call directory seeded; idempotent per `up`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
