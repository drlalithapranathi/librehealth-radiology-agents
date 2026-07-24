"""MIMIC-CXR DICOM fix-up for the showcase ETL (#68): make each study joinable by the pipeline.

MIMIC-CXR DICOMs are de-identified and ship WITHOUT an AccessionNumber. The ingress joins a study
to its workflow (and, post-#70, resolves the order) ONLY on the DICOM AccessionNumber -- so a blank
accession means `Patient/UNRESOLVED` and a study that never gets its EHR context or a joinable
sign-off. This tool makes the tag load-bearing.

Rules (build item 2 of #68):
- AccessionNumber: ENFORCED to the MIMIC study_id (e.g. `s56699142`), overwriting any shipped
  value. Live MIMIC-CXR v2.1.0 files carry a numeric de-identified accession (the study_id
  digits), not a blank -- and an order can only ever be keyed on the canonical form, so keeping
  the shipped value silently breaks the ingest join for every study (found live, 2026-07-24).
  A replaced value is recorded in the result's warnings.
- StudyDescription: an explicit per-study override (the manifest's description) wins; otherwise
  if blank, use PerformedProcedureStepDescription, else a sensible CXR default. Live MIMIC files
  ship StudyDescription blank with the deid placeholder "Performed Desc" in PPSD, which the
  interpretation registry (#62/#64) and the viewer's CXR hanging protocol cannot select on --
  pass the override for real runs.
- StudyInstanceUID: NEVER touched -- kept exactly as shipped (it maps to the Orthanc study id).

This mutates de-identified data only; it adds no PHI. No MIMIC data lives in the repo (DUA).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

# Defaults when the source carries no procedure description. Two-view frontal is the common CXR;
# portable studies are AP. The ETL can pass an explicit default per study from the cohort manifest.
DEFAULT_DESCRIPTION = "CHEST (PA AND LAT)"
PORTABLE_DESCRIPTION = "CHEST (PORTABLE AP)"


def study_id_to_accession(study_id: str) -> str:
    """The one place the DICOM<->order accession convention lives: the MIMIC study_id verbatim
    (`s56699142`). Both the DICOM fix-up here and the RadiologyOrder loader MUST call this, or the
    ingest join silently misses."""
    return str(study_id).strip()


def _blank(value) -> bool:
    return value is None or str(value).strip() == ""


@dataclass
class FixupResult:
    changed: list[str] = field(default_factory=list)
    accession: Optional[str] = None
    description: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def fixup_dataset(ds, study_id: str, default_description: str = DEFAULT_DESCRIPTION,
                  description_override: Optional[str] = None) -> FixupResult:
    """Mutate a pydicom Dataset in place per the rules above; return what changed.

    `ds` is a pydicom Dataset. `study_id` is the MIMIC study id for this study. Idempotent:
    re-running on an already-fixed file changes nothing.
    """
    out = FixupResult()

    # StudyInstanceUID must exist and is never altered -- it is the Orthanc study identity.
    if _blank(getattr(ds, "StudyInstanceUID", None)):
        out.warnings.append("StudyInstanceUID is blank; refusing to fabricate one (kept as-is)")

    want_acc = study_id_to_accession(study_id)
    have_acc = str(getattr(ds, "AccessionNumber", "") or "").strip()
    if have_acc != want_acc:
        ds.AccessionNumber = want_acc
        out.changed.append(f"AccessionNumber -> {want_acc}")
        if have_acc:
            # The shipped value is a de-identified artifact; nothing downstream can key on it,
            # so enforcing the canonical accession loses no real data. Record the replacement.
            out.warnings.append(f"AccessionNumber was {have_acc!r}; overwrote with {want_acc!r}")
    out.accession = str(getattr(ds, "AccessionNumber", "")).strip()

    have_desc = str(getattr(ds, "StudyDescription", "") or "").strip()
    if description_override and have_desc != description_override.strip():
        ds.StudyDescription = description_override.strip()
        out.changed.append(f"StudyDescription -> {description_override.strip()}")
    elif _blank(have_desc):
        ppsd = getattr(ds, "PerformedProcedureStepDescription", None)
        desc = str(ppsd).strip() if not _blank(ppsd) else default_description
        ds.StudyDescription = desc
        out.changed.append(f"StudyDescription -> {desc}")
    out.description = str(getattr(ds, "StudyDescription", "")).strip()

    return out


def fixup_file(path: str, study_id: str, default_description: str = DEFAULT_DESCRIPTION,
               out_path: Optional[str] = None,
               description_override: Optional[str] = None) -> FixupResult:
    """Read a DICOM file, fix it up, and write it back (in place unless `out_path` is given).

    With `out_path` the file is ALWAYS written, changed or not: callers stream the output file
    onward, and skipping the write would hand them whatever the previous iteration left there.
    """
    import pydicom  # local import: the ETL tooling depends on pydicom, the rest of the repo does not
    ds = pydicom.dcmread(path)
    result = fixup_dataset(ds, study_id, default_description, description_override)
    if result.changed or out_path:
        ds.save_as(out_path or path)
    return result


def _main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Fix up a MIMIC-CXR DICOM for pipeline ingestion (#68).")
    p.add_argument("path", help="DICOM file to fix up")
    p.add_argument("study_id", help="MIMIC study id, e.g. s56699142")
    p.add_argument("--description", default=DEFAULT_DESCRIPTION, help="fallback StudyDescription")
    p.add_argument("--set-description", default=None,
                   help="authoritative StudyDescription (e.g. the manifest's); overwrites")
    p.add_argument("--out", default=None, help="write here instead of in place")
    args = p.parse_args(argv)
    r = fixup_file(args.path, args.study_id, args.description, args.out, args.set_description)
    for c in r.changed:
        print("changed:", c)
    for w in r.warnings:
        print("WARN:", w)
    print(f"accession={r.accession} description={r.description!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
