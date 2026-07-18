"""Curate the ~100-study MIMIC-CXR showcase cohort into a manifest (#68).

Joins the MIMIC-CXR-JPG label files to the study list, filters to studies whose report carries
both FINDINGS and IMPRESSION (report_body.py parses those headers, so a study without them is
useless downstream), and fills the #68 composition buckets:

  normals 30 / pneumothorax 25 / effusion-consolidation-edema 20 / with-priors 15 / portable 10

Label policy: a study counts as positive (or as a normal) only when chexpert.csv AND negbio.csv
AGREE (both 1.0). Discordant studies are left out of the labelled buckets; the demo's concordance
story is cleaner when the cohort labels are not themselves in dispute.

The EHR packet comes from the MIMIC-IV hosp CSVs for the selected subjects: labevents creatinine
(itemid 50912 -> LOINC 2160-0), prescriptions filtered to anticoagulants (the med-flag rules), and
diagnoses_icd (ICD-10 only, dot-normalised: J95811 -> J95.811). Order metadata: `portable` from the
metadata CSV's PerformedProcedureStepDescription/ViewPosition; `reason_codes` from a rule-out
indication in the report joined with the subject's J93*/J95.811 diagnoses; priority stat for
rule-out-pneumothorax indications, urgent for other pneumothorax positives, else routine.

DUA: the manifest CONTAINS MIMIC content (report text), so --out must stay off this repo, exactly
like fetch.py's dest. Inputs live on the access-controlled demo host.

Typical run (demo host):
  python curate_cohort.py --cxr-root /secure/mimic-cxr --reports-root /secure/mimic-cxr-reports \
      --mimic-iv-root /secure/mimic-iv --out /secure/cohort/showcase_cohort.json
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import argparse
import csv
import gzip
import io
import json
import os
import re

CHEXPERT_LABELS = [
    "Atelectasis", "Cardiomegaly", "Consolidation", "Edema", "Enlarged Cardiomediastinum",
    "Fracture", "Lung Lesion", "Lung Opacity", "No Finding", "Pleural Effusion", "Pleural Other",
    "Pneumonia", "Pneumothorax", "Support Devices",
]
EFFUSION_GROUP = ("Pleural Effusion", "Consolidation", "Edema")
# THERAPEUTIC anticoagulants the med-flag verification rules care about (bleeding risk). These are
# therapeutic at any route (oral warfarin/DOACs, LMWH, parenteral DTIs), matched as substrings of
# MIMIC-IV prescriptions.drug (lowercased).
NON_HEPARIN_ANTICOAGULANTS = ("warfarin", "enoxaparin", "apixaban", "rivaroxaban", "dabigatran",
                              "edoxaban", "fondaparinux", "argatroban", "bivalirudin")
# Heparin is the hard case: MIMIC prescriptions are dominated by heparin that is NOT anticoagulating
# the patient -- subcutaneous DVT prophylaxis (near-universal inpatient), line flushes, catheter
# dwells, CRRT/dialysis circuit heparin. Only an IV heparin DRIP is therapeutic. So heparin counts
# only when the route is intravenous AND the drug string is not a line-maintenance form.
IV_ROUTE_TOKENS = ("iv", "intravenous")
ANTICOAGULANT_EXCLUDE = ("flush", "lock", "dwell", "crrt", "priming", "hemodialysis", "dialysis",
                         "catheter")


def is_therapeutic_anticoagulant(drug: str, route: str) -> bool:
    low = (drug or "").lower()
    if any(x in low for x in ANTICOAGULANT_EXCLUDE):
        return False
    if any(a in low for a in NON_HEPARIN_ANTICOAGULANTS):
        return True
    if "heparin" in low:
        return any(t in (route or "").lower() for t in IV_ROUTE_TOKENS)
    return False
CREATININE_ITEMID = "50912"           # MIMIC-IV labevents itemid -> LOINC 2160-0
CREATININE_LOINC = "2160-0"

DEFAULT_TARGETS = {"normal": 30, "pneumothorax": 25, "effusion": 20, "priors": 15, "portable": 10}


# --- report sections ---------------------------------------------------------
_HEADER_RE = re.compile(r"^\s*([A-Z][A-Z /()]+?):\s*", re.MULTILINE)


def parse_sections(text: str) -> dict:
    """MIMIC report -> {HEADER: body}. Headers are the ALL-CAPS colon lines (FINDINGS:,
    IMPRESSION:, INDICATION:, ...). Text before the first header is ignored."""
    sections: dict[str, str] = {}
    matches = list(_HEADER_RE.finditer(text or ""))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        header = re.sub(r"\s+", " ", m.group(1)).strip()
        body = text[m.end():end].strip()
        if body:
            sections[header] = body
    return sections


def has_findings_and_impression(text: str) -> bool:
    s = parse_sections(text)
    return bool(s.get("FINDINGS")) and bool(s.get("IMPRESSION"))


def rule_out_pneumothorax(indication: str) -> bool:
    """A post-procedure 'rule out pneumothorax' indication: fires the J95.811 reason-code slice
    on the ORDER (#68 cohort note). Wording heuristic over the INDICATION section."""
    t = (indication or "").lower()
    if "pneumothorax" not in t and "ptx" not in t:
        return False
    return bool(re.search(r"rule\s*out|r/o|eval|assess|status\s*post|s/p|post|line|tube|catheter|"
                          r"placement|biopsy|thoracentesis|procedure", t))


# --- ICD-10 -----------------------------------------------------------------
def dot_icd10(code: str) -> str:
    """MIMIC-IV stores ICD-10 undotted (J95811); OpenMRS/ICD-10 convention wants J95.811."""
    code = (code or "").strip().upper()
    if len(code) > 3 and "." not in code:
        return code[:3] + "." + code[3:]
    return code


def is_pneumothorax_code(code: str) -> bool:
    c = dot_icd10(code)
    return c.startswith("J93") or c == "J95.811"


# --- inputs ------------------------------------------------------------------
def _sid(v: str) -> str:
    v = str(v).strip()
    return v if v.startswith("s") else f"s{v}"


def _open_maybe_gz(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, newline="", encoding="utf-8")


def read_labels(path: str) -> dict:
    """chexpert.csv / negbio.csv -> {study_id: {label: float}} (blank cells omitted)."""
    out: dict[str, dict] = {}
    with _open_maybe_gz(path) as f:
        for row in csv.DictReader(f):
            labels = {}
            for name in CHEXPERT_LABELS:
                cell = (row.get(name) or "").strip()
                if cell:
                    labels[name] = float(cell)
            out[_sid(row["study_id"])] = labels
    return out


def read_study_list(path: str) -> list[dict]:
    """cxr-study-list.csv -> [{study_id, subject_id}] in file order."""
    with _open_maybe_gz(path) as f:
        return [{"study_id": _sid(r["study_id"]), "subject_id": str(r["subject_id"]).strip()}
                for r in csv.DictReader(f)]


def read_metadata(path: str) -> dict:
    """mimic-cxr-2.0.0-metadata.csv -> {study_id: {portable, study_date}} (per-study collapse:
    portable if ANY record says so; earliest StudyDate stands for the study)."""
    out: dict[str, dict] = {}
    with _open_maybe_gz(path) as f:
        for r in csv.DictReader(f):
            sid = _sid(r["study_id"])
            desc = (r.get("PerformedProcedureStepDescription") or "").upper()
            view = (r.get("ViewPosition") or "").upper()
            portable = "PORT" in desc or view == "AP"
            date = (r.get("StudyDate") or "").strip()
            cur = out.setdefault(sid, {"portable": False, "study_date": date})
            cur["portable"] = cur["portable"] or portable
            if date and (not cur["study_date"] or date < cur["study_date"]):
                cur["study_date"] = date
    return out


def report_path(reports_root: str, subject_id: str, study_id: str) -> str:
    subj = str(subject_id).lstrip("p")
    return os.path.join(reports_root, "files", f"p{subj[:2]}", f"p{subj}", f"{_sid(study_id)}.txt")


# --- MIMIC-IV slices ---------------------------------------------------------
def read_diagnoses(path: str, subjects: set) -> dict:
    """diagnoses_icd -> {subject_id: [dotted ICD-10 codes, seq order, deduped]}."""
    out: dict[str, list] = {}
    with _open_maybe_gz(path) as f:
        for r in csv.DictReader(f):
            subj = str(r["subject_id"]).strip()
            if subj not in subjects or str(r.get("icd_version", "")).strip() != "10":
                continue
            code = dot_icd10(r["icd_code"])
            bucket = out.setdefault(subj, [])
            if code not in bucket:
                bucket.append(code)
    return out


def read_prescriptions(path: str, subjects: set) -> dict:
    """prescriptions -> {subject_id: [therapeutic anticoagulant drug names, deduped]}. Prophylactic
    subcutaneous heparin, line flushes and circuit heparin are excluded (see
    is_therapeutic_anticoagulant); an IV heparin drip counts."""
    out: dict[str, list] = {}
    with _open_maybe_gz(path) as f:
        for r in csv.DictReader(f):
            subj = str(r["subject_id"]).strip()
            if subj not in subjects:
                continue
            drug = (r.get("drug") or "").strip()
            if is_therapeutic_anticoagulant(drug, r.get("route", "")):
                bucket = out.setdefault(subj, [])
                if drug not in bucket:
                    bucket.append(drug)
    return out


def read_creatinine(path: str, subjects: set, per_subject: int = 2) -> dict:
    """labevents (streamed; the file is huge) -> {subject_id: [{value, unit, date}]} for
    creatinine rows with a numeric value. Keeps the last `per_subject` per subject."""
    out: dict[str, list] = {}
    with _open_maybe_gz(path) as f:
        for r in csv.DictReader(f):
            if r.get("itemid", "").strip() != CREATININE_ITEMID:
                continue
            subj = str(r["subject_id"]).strip()
            if subj not in subjects:
                continue
            raw = (r.get("valuenum") or "").strip()
            if not raw:
                continue
            bucket = out.setdefault(subj, [])
            bucket.append({"value": float(raw), "unit": (r.get("valueuom") or "").strip(),
                           "date": (r.get("charttime") or "").strip()})
            del bucket[:-per_subject]
    return out


def read_genders(path: str, subjects: set) -> dict:
    with _open_maybe_gz(path) as f:
        return {str(r["subject_id"]).strip(): (r.get("gender") or "U").strip()
                for r in csv.DictReader(f) if str(r["subject_id"]).strip() in subjects}


# --- selection ---------------------------------------------------------------
@dataclass
class Candidate:
    study_id: str
    subject_id: str
    report_text: str = ""
    sections: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)   # concordant labels only
    portable: bool = False
    study_date: str = ""
    prior_study_ids: list = field(default_factory=list)


def concordant(chex: dict, neg: dict) -> dict:
    """Labels where chexpert and negbio agree at 1.0 (positives) or both No Finding at 1.0."""
    return {k: 1 for k, v in (chex or {}).items() if v == 1.0 and (neg or {}).get(k) == 1.0}


def bucket_of(c: Candidate) -> Optional[str]:
    if c.labels.get("Pneumothorax"):
        return "pneumothorax"
    if any(c.labels.get(l) for l in EFFUSION_GROUP):
        return "effusion"
    if c.labels.get("No Finding"):
        return "normal"
    return None


def select(cands: list, targets: dict, max_per_subject: int = 2) -> dict:
    """Fill the composition buckets, each study counted once. Label buckets first (pneumothorax,
    effusion, normal), then the cross-cutting priors and portable quotas from what remains.
    `max_per_subject` keeps the cohort from clumping on prolific subjects (the study list is
    subject-ordered, so unbounded greedy selection drains one patient at a time); the priors
    bucket is exempt since a priors story NEEDS same-subject studies. Returns {bucket: [Candidate]}."""
    chosen: dict[str, list] = {k: [] for k in targets}
    taken: set[str] = set()
    per_subject: dict[str, int] = {}

    def _take(bucket: str, c: Candidate, capped: bool = True) -> None:
        if capped and per_subject.get(c.subject_id, 0) >= max_per_subject:
            return
        chosen[bucket].append(c)
        taken.add(c.study_id)
        per_subject[c.subject_id] = per_subject.get(c.subject_id, 0) + 1

    for bucket in ("pneumothorax", "effusion", "normal"):
        for c in cands:
            if len(chosen[bucket]) >= targets[bucket]:
                break
            if c.study_id in taken or bucket_of(c) != bucket:
                continue
            _take(bucket, c)
    for c in cands:
        if len(chosen["priors"]) >= targets["priors"]:
            break
        if c.study_id not in taken and c.prior_study_ids:
            _take("priors", c, capped=False)
    for c in cands:
        if len(chosen["portable"]) >= targets["portable"]:
            break
        if c.study_id not in taken and c.portable:
            _take("portable", c)
    return chosen


def to_manifest_entry(c: Candidate, diagnoses: dict, meds: dict, labs: dict, genders: dict) -> dict:
    subj = c.subject_id
    indication = c.sections.get("INDICATION", "")
    subject_ptx_codes = [x for x in diagnoses.get(subj, []) if is_pneumothorax_code(x)]
    reason_codes: list[str] = []
    if rule_out_pneumothorax(indication):
        reason_codes = subject_ptx_codes or ["J95.811"]
    priority = "routine"
    if c.labels.get("Pneumothorax"):
        priority = "stat" if reason_codes else "urgent"
    entry = {
        "study_id": c.study_id,
        "subject_id": subj,
        "description": "CHEST (PORTABLE AP)" if c.portable else "CHEST (PA AND LAT)",
        "portable": c.portable,
        "priority": priority,
        "report_text": c.report_text,
        "labels": dict(c.labels, gender=genders.get(subj, "U")),
        "labs": [{"code": CREATININE_LOINC, "value": l["value"], "unit": l["unit"],
                  "date": l["date"], "display": "Creatinine"} for l in labs.get(subj, [])],
        "meds": [{"display": d} for d in meds.get(subj, [])],
        "problems": [{"code": x} for x in diagnoses.get(subj, [])[:5]],
    }
    if reason_codes:
        entry["reason_codes"] = reason_codes
    if c.prior_study_ids:
        entry["prior_study_ids"] = c.prior_study_ids
    return entry


# --- CLI ---------------------------------------------------------------------
def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _input_path(root: str, *names: str) -> Optional[str]:
    """First existing input among the candidate names, each also tried with .gz (the PhysioNet
    distributions ship gzipped and version-prefixed: mimic-cxr-2.0.0-chexpert.csv.gz)."""
    for name in names:
        for candidate in (name, name + ".gz"):
            p = os.path.join(root, candidate)
            if os.path.exists(p):
                return p
    return None


def build_candidates(study_list: list, chex: dict, neg: dict, meta: dict,
                     reports_root: str) -> tuple:
    """Study list -> candidates with a parseable report, plus skip counters. Priors are derived
    from the per-subject StudyDate ordering (earlier study of the same subject = a prior)."""
    by_subject: dict[str, list] = {}
    skipped = {"no_report": 0, "no_sections": 0}
    cands = []
    for row in study_list:
        sid, subj = row["study_id"], row["subject_id"]
        path = report_path(reports_root, subj, sid)
        if not os.path.exists(path):
            skipped["no_report"] += 1
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        sections = parse_sections(text)
        if not (sections.get("FINDINGS") and sections.get("IMPRESSION")):
            skipped["no_sections"] += 1
            continue
        m = meta.get(sid, {})
        c = Candidate(study_id=sid, subject_id=subj, report_text=text.strip(), sections=sections,
                      labels=concordant(chex.get(sid), neg.get(sid)),
                      portable=bool(m.get("portable")), study_date=m.get("study_date", ""))
        cands.append(c)
        by_subject.setdefault(subj, []).append(c)
    for peers in by_subject.values():
        peers.sort(key=lambda c: (c.study_date, c.study_id))
        for i, c in enumerate(peers):
            c.prior_study_ids = [p.study_id for p in peers[:i]]
    return cands, skipped


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Curate the #68 MIMIC-CXR showcase cohort manifest.")
    p.add_argument("--cxr-root", required=True, help="MIMIC-CXR-JPG root (label + list CSVs)")
    p.add_argument("--reports-root", default="", help="root holding files/pXX/pYYY/sZZZ.txt "
                                                      "(default: --cxr-root)")
    p.add_argument("--mimic-iv-root", default="", help="MIMIC-IV hosp/ root for the EHR slices "
                                                       "(omit to curate without labs/meds/problems)")
    p.add_argument("--out", required=True, help="manifest path, OFF this repo (DUA)")
    p.add_argument("--max-per-subject", type=int, default=2,
                   help="cap studies per subject outside the priors bucket (subject diversity)")
    for k, v in DEFAULT_TARGETS.items():
        p.add_argument(f"--{k}", type=int, default=v)
    args = p.parse_args(argv)

    out_abs = os.path.abspath(args.out)
    if out_abs.startswith(_repo_root() + os.sep):
        p.error("--out is inside the repo; the manifest carries MIMIC content (DUA), keep it off")

    root = args.cxr_root
    reports_root = args.reports_root or root
    chex_path = _input_path(root, "chexpert.csv", "mimic-cxr-2.0.0-chexpert.csv",
                            "mimic-cxr-2.1.0-chexpert.csv")
    neg_path = _input_path(root, "negbio.csv", "mimic-cxr-2.0.0-negbio.csv",
                           "mimic-cxr-2.1.0-negbio.csv")
    list_path = _input_path(root, "cxr-study-list.csv")
    if not (chex_path and neg_path and list_path):
        p.error(f"missing label/list CSVs under {root} (need chexpert, negbio, cxr-study-list)")
    chex = read_labels(chex_path)
    neg = read_labels(neg_path)
    study_list = read_study_list(list_path)
    meta_path = _input_path(root, "mimic-cxr-2.0.0-metadata.csv", "mimic-cxr-2.1.0-metadata.csv")
    meta = read_metadata(meta_path) if meta_path else {}

    cands, skipped = build_candidates(study_list, chex, neg, meta, reports_root)
    targets = {k: getattr(args, k) for k in DEFAULT_TARGETS}
    chosen = select(cands, targets, max_per_subject=args.max_per_subject)
    picked = [c for bucket in chosen.values() for c in bucket]
    subjects = {c.subject_id for c in picked}

    diagnoses = meds = labs = genders = {}
    if args.mimic_iv_root:
        hosp = os.path.join(args.mimic_iv_root, "hosp")

        def _first(name):
            for cand in (os.path.join(hosp, name), os.path.join(hosp, name + ".gz"),
                         os.path.join(args.mimic_iv_root, name),
                         os.path.join(args.mimic_iv_root, name + ".gz")):
                if os.path.exists(cand):
                    return cand
            return None

        path = _first("diagnoses_icd.csv")
        diagnoses = read_diagnoses(path, subjects) if path else {}
        path = _first("prescriptions.csv")
        meds = read_prescriptions(path, subjects) if path else {}
        path = _first("labevents.csv")
        labs = read_creatinine(path, subjects) if path else {}
        path = _first("patients.csv")
        genders = read_genders(path, subjects) if path else {}

    studies = [to_manifest_entry(c, diagnoses, meds, labs, genders) for c in picked]
    with open(out_abs, "w", encoding="utf-8") as f:
        json.dump({"studies": studies}, f, indent=2)

    print(f"candidates: {len(cands)} usable ({skipped['no_report']} without a report file, "
          f"{skipped['no_sections']} without FINDINGS+IMPRESSION)")
    for k in DEFAULT_TARGETS:
        print(f"  {k:12} {len(chosen[k]):3}/{targets[k]}")
    with_reason = sum(1 for s in studies if s.get("reason_codes"))
    with_meds = sum(1 for s in studies if s["meds"])
    print(f"selected {len(studies)} studies from {len(subjects)} subjects; "
          f"{with_reason} with order reason codes, {with_meds} with anticoagulant meds")
    print(f"wrote {out_abs} (keep OFF the repo; DUA)")
    short = [k for k in DEFAULT_TARGETS if len(chosen[k]) < targets[k]]
    if short:
        print(f"WARNING: buckets under target: {short}; widen the candidate pool or lower targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
