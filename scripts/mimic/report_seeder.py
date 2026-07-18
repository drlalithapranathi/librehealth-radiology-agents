"""Report seeding + the flip-to-final rehearsal cue (#68 build item 4).

Seeding (a `preliminary` DiagnosticReport basedOn the order) happens in load_cohort. This tool adds
the rehearsal cue: `finalize <accession>` flips that study's seeded report to `final`, which makes
the RIS poller fire `report_finalized` and drive the human-gated sign-off loop WITHOUT a live RIS
sign. In the live demo, radiologists sign in the RIS instead and this tool is not used.

Proven end to end (#68): an order loaded for accession `s68proof1`, its DICOM pushed and the
workflow parked at AWAITING_RADIOLOGIST, then `finalize s68proof1` released the gate.
"""
from __future__ import annotations
import argparse

from omrs_client import OmrsClient


def finalize(c: OmrsClient, accession: str) -> str:
    order = c.order_for_accession(accession)
    if not order:
        raise SystemExit(f"no RadiologyOrder for accession {accession!r} (load the FHIR side first)")
    report_id = c.find_seeded_report(order["patient_uuid"], order["order_uuid"])
    if not report_id:
        raise SystemExit(f"no seeded report basedOn ServiceRequest/{order['order_uuid']}")
    c.finalize_diagnostic_report(report_id)
    return report_id


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Flip a seeded report to final (rehearsal sign-off cue).")
    p.add_argument("accession", help="the study accession (MIMIC study_id)")
    args = p.parse_args(argv)
    rid = finalize(OmrsClient(), args.accession)
    print(f"finalized DiagnosticReport/{rid} for accession {args.accession} "
          f"-> the RIS poller will detect report_finalized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
