"""Selective fetch of a MIMIC-CXR cohort from PhysioNet S3 (#68 build item 1).

Pulls ONLY the manifest's study folders (DICOM + report), never the full 4.7 TB. Requires PhysioNet
credentialed AWS access (the demo host has it). Nothing is downloaded into this repo -- the DUA
forbids redistribution, so `--dest` must point off-repo (an access-controlled path).

MIMIC-CXR S3 layout: files/p<NN>/p<subject_id>/s<study_id>/*.dcm  (+ s<study_id>.txt report), where
p<NN> is 'p' + the first two digits of the subject id. Keys derive from the manifest, so no crawl.
"""
from __future__ import annotations
import argparse
import os

from manifest import load_manifest, CohortStudy

DEFAULT_BUCKET = os.environ.get("MIMIC_CXR_BUCKET", "mimic-cxr-2.0.0.physionet.org")


def study_prefix(s: CohortStudy) -> str:
    subj = str(s.subject_id).lstrip("p")
    study = str(s.study_id).lstrip("s")
    return f"files/p{subj[:2]}/p{subj}/s{study}/"


def fetch_study(s3, bucket: str, s: CohortStudy, dest: str) -> list[str]:
    prefix = study_prefix(s)
    out = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            local = os.path.join(dest, key)
            os.makedirs(os.path.dirname(local), exist_ok=True)
            s3.download_file(bucket, key, local)
            out.append(local)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Fetch a MIMIC-CXR cohort slice from PhysioNet S3 (#68).")
    p.add_argument("manifest")
    p.add_argument("dest", help="off-repo download dir (access-controlled; DUA)")
    p.add_argument("--bucket", default=DEFAULT_BUCKET)
    args = p.parse_args(argv)
    import boto3  # local: only the fetch tool needs AWS
    s3 = boto3.client("s3")
    total = 0
    for s in load_manifest(args.manifest):
        files = fetch_study(s3, args.bucket, s, args.dest)
        total += len(files)
        print(f"{s.study_id}: {len(files)} objects under {study_prefix(s)}")
    print(f"\nfetched {total} objects to {args.dest} (keep OFF the repo -- DUA)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
