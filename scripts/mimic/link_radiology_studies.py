"""Link cohort orders to their pushed DICOM studies so the RIS report surface exists (#76 A.2).

Third ETL phase, run AFTER load_cohort (FHIR side) and AFTER the DICOM push to
Orthanc. For each manifest study: look up the pushed study in Orthanc by
AccessionNumber, read its REAL StudyInstanceUID, and upsert the
``radiology_study`` row (performed_status COMPLETED) on the matching order.
Without that row the radiology module shows no Report segment on the order
page -- no Claim Report, no report authoring, no demo arc 2.

Ordered after the push on purpose: the StudyInstanceUID is never fabricated
(dicom_fixup keeps the shipped UID, and the RIS View Study link joins on it),
so it can only come from what actually landed in Orthanc.

Best-effort per study, like the rest of the loader: a study whose DICOM has
not landed yet is a warning, not a failure -- re-run after the push completes.

Run: python link_radiology_studies.py cohort.json
Env: ORTHANC_BASE_URL (default http://orthanc:8042), plus the omrs_client DB/REST vars.
"""
from __future__ import annotations

import argparse
import os

import httpx

from dicom_fixup import study_id_to_accession
from manifest import load_manifest
from omrs_client import OmrsClient


def find_study_uid_in_orthanc(accession: str, base_url: str,
                              http: httpx.Client) -> str | None:
    """The pushed study's StudyInstanceUID, from Orthanc's accession index."""
    r = http.post(f"{base_url}/tools/find",
                  json={"Level": "Study", "Expand": True,
                        "Query": {"AccessionNumber": accession}})
    r.raise_for_status()
    matches = r.json()
    if not matches:
        return None
    return matches[0].get("MainDicomTags", {}).get("StudyInstanceUID") or None


def link_studies(studies, client: OmrsClient, orthanc_base_url: str,
                 http: httpx.Client, find_uid=find_study_uid_in_orthanc) -> dict:
    """Link every manifest study; returns {'linked': n, 'warnings': [...]}."""
    summary: dict = {"linked": 0, "warnings": []}
    for s in studies:
        accession = study_id_to_accession(s.study_id)
        try:
            uid = find_uid(accession, orthanc_base_url, http)
            if not uid:
                summary["warnings"].append(
                    f"{accession}: no study in Orthanc (DICOM not pushed yet?)")
                continue
            if client.ensure_radiology_study(accession, uid) is None:
                summary["warnings"].append(
                    f"{accession}: no RadiologyOrder (run load_cohort first)")
                continue
            summary["linked"] += 1
        except Exception as e:  # noqa: BLE001 - best-effort per study
            summary["warnings"].append(f"{accession}: {type(e).__name__}: {e}")
    return summary


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("manifest", help="cohort manifest JSON (same file load_cohort used)")
    p.add_argument("--orthanc", default=os.environ.get("ORTHANC_BASE_URL", "http://orthanc:8042"))
    args = p.parse_args(argv)

    studies = load_manifest(args.manifest)
    client = OmrsClient()
    with httpx.Client(timeout=30.0) as http:
        summary = link_studies(studies, client, args.orthanc.rstrip("/"), http)
    client.close()

    for w in summary["warnings"]:
        print(f"WARN {w}")
    print(f"\n{summary['linked']}/{len(studies)} studies linked "
          f"(radiology_study rows with real StudyInstanceUIDs).")
    return 0 if summary["linked"] == len(studies) else 1


if __name__ == "__main__":
    raise SystemExit(main())
