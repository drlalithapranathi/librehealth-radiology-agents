"""Export the distinct (Modality, StudyDescription) the LOADED cohort actually carries, so the
interpretation-registry selection test runs against real Orthanc values, not invented ones
(#68 build item 5, refs #64).

Reads the studies from Orthanc (post-load), collects the distinct pairs, and writes a corpus JSON
that scripts/audit_registry_selection.py / the registry selection test can consume.
"""
from __future__ import annotations
import argparse
import json
import os
import httpx

ORTHANC = os.environ.get("ORTHANC_BASE_URL", "http://localhost:8042")


def collect(orthanc_base: str) -> list[dict]:
    pairs = {}
    with httpx.Client(timeout=30) as c:
        ids = c.get(f"{orthanc_base}/studies").json()
        for sid in ids:
            study = c.get(f"{orthanc_base}/studies/{sid}").json()
            desc = study.get("MainDicomTags", {}).get("StudyDescription", "")
            # modality lives on series; read one series' modality
            series = study.get("Series", [])
            modality = ""
            if series:
                modality = c.get(f"{orthanc_base}/series/{series[0]}").json() \
                    .get("MainDicomTags", {}).get("Modality", "")
            key = (modality, desc)
            pairs.setdefault(key, 0)
            pairs[key] += 1
    return [{"modality": m, "studyDescription": d, "count": n} for (m, d), n in sorted(pairs.items())]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Export loaded (modality, StudyDescription) corpus (#68/#64).")
    p.add_argument("--orthanc", default=ORTHANC)
    p.add_argument("--out", default="registry_corpus.json")
    args = p.parse_args(argv)
    corpus = collect(args.orthanc)
    with open(args.out, "w") as f:
        json.dump({"studyTypes": corpus}, f, indent=2)
    print(f"wrote {len(corpus)} distinct (modality, description) pairs to {args.out}")
    for row in corpus:
        print(f"  {row['modality'] or '?':4} | {row['studyDescription'] or '(none)'} x{row['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
