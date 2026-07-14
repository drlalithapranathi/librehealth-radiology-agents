#!/usr/bin/env python3
"""Push a REAL chest X-ray into the dev Orthanc so the pipeline has something to actually score.

    python scripts/load_demo_cxr.py                       # -> http://localhost:8042
    python scripts/load_demo_cxr.py --url http://orthanc:8042

Until #27 there was no reason for a real image to exist anywhere in this repo: every tool was a
stub, so a StudyContext with a plausible StudyDescription was enough to exercise the whole pipeline.
`cxr-screen` now runs a real classifier over real pixels, and a stubbed fixture cannot demonstrate
that -- or catch it being wrong.

THE IMAGE. pydicom's test corpus ships RG1_UNCR.dcm: a genuine CR CHEST radiograph (1955x1841).
We do NOT vendor it into this repo -- pydicom fetches and caches it (~7MB, once, to ~/.pydicom),
so there is nothing here to relicense or keep in sync. First run needs network; later runs do not.

It is also MONOCHROME1, which is a happy accident worth stating plainly: it is exactly the case
radagent_common.imaging inverts. Scored WITHOUT that inversion, this film reports
  Nodule 0.09, Lung Opacity 0.28, Consolidation 0.11   (all "negative")
and scored correctly it reports
  Nodule 0.61, Lung Opacity 0.77, Consolidation 0.51   (all positive).
So the demo study is, by luck, also the regression test: if the pipeline ever loses the inversion,
the headline demo silently turns four positives into false negatives, and nothing raises.

Its StudyDescription is "THORAX", which the registry's chest aliases (#63) already match -- so the
study selects cxr-screen + pneumothorax-detect with no tag rewriting. Nothing here fakes the inputs.
"""
from __future__ import annotations

import argparse
import sys

DEFAULT_URL = "http://localhost:8042"
TEST_FILE = "RG1_UNCR.dcm"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=DEFAULT_URL, help=f"Orthanc REST base (default {DEFAULT_URL})")
    args = ap.parse_args()

    try:
        import httpx
        from pydicom.data import get_testdata_file
    except ModuleNotFoundError as exc:
        print(f"needs the imaging extra: pip install -e 'libs/radagent-common[imaging]' httpx ({exc})")
        return 2

    print(f"fetching {TEST_FILE} (real CR CHEST; ~7MB, cached in ~/.pydicom after the first run)")
    path = get_testdata_file(TEST_FILE)
    if not path:
        print(f"could not obtain {TEST_FILE} from the pydicom test corpus")
        return 1
    dicom = open(path, "rb").read()

    print(f"uploading to Orthanc at {args.url}")
    try:
        r = httpx.post(
            f"{args.url.rstrip('/')}/instances",
            content=dicom,
            headers={"Content-Type": "application/dicom"},
            timeout=60.0,
        )
        r.raise_for_status()
    except Exception as exc:
        print(f"upload failed: {exc}")
        print("is the stack up?  docker compose ps orthanc")
        return 1

    body = r.json()
    # Orthanc returns the ids it assigned; the study id is what the orchestrator keys a workflow on.
    study_id = body.get("ParentStudy", "")
    print("\nuploaded.")
    print(f"  orthancStudyId : {study_id}")
    print(f"  instance       : {body.get('ID', '')}")
    print(f"  status         : {body.get('Status', '')}")
    print("\nOrthanc fires its stable-study webhook to the orchestrator once the study settles")
    print("(StableAge). Watch it flow:")
    print("  docker compose logs -f orchestrator | grep -i interpretation")
    print("  open http://localhost:8233        # Temporal UI: the workflow for this study")
    return 0


if __name__ == "__main__":
    sys.exit(main())
