"""Unit tests for the cohort curation tool (#68). Synthetic data only, built in-test."""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import curate_cohort as C  # noqa: E402

REPORT_OK = """EXAMINATION: CHEST (PA AND LAT)

INDICATION: Central line placement, rule out pneumothorax.

FINDINGS: Small right apical pneumothorax. Line tip at the cavoatrial junction.

IMPRESSION: Small right apical pneumothorax.
"""
REPORT_NO_SECTIONS = "Chest x-ray. Lungs clear. No acute process."


# --- report sections ------------------------------------------------------
def test_parse_sections_extracts_headers():
    s = C.parse_sections(REPORT_OK)
    assert "pneumothorax" in s["FINDINGS"].lower()
    assert s["IMPRESSION"].startswith("Small right apical")
    assert "rule out" in s["INDICATION"].lower()


def test_findings_and_impression_gate():
    assert C.has_findings_and_impression(REPORT_OK)
    assert not C.has_findings_and_impression(REPORT_NO_SECTIONS)


def test_rule_out_pneumothorax_heuristic():
    assert C.rule_out_pneumothorax("Central line placement, rule out pneumothorax.")
    assert C.rule_out_pneumothorax("s/p thoracentesis, eval for ptx")
    assert not C.rule_out_pneumothorax("Cough and fever.")
    # a bare mention with no procedural or rule-out wording does not fire
    assert not C.rule_out_pneumothorax("History of pneumothorax in 2019.")


# --- ICD-10 ---------------------------------------------------------------
def test_dot_icd10_normalises_mimic_codes():
    assert C.dot_icd10("J95811") == "J95.811"
    assert C.dot_icd10("J90") == "J90"          # 3-char codes take no dot
    assert C.dot_icd10("j93.0") == "J93.0"      # already dotted stays put


def test_pneumothorax_code_family():
    assert C.is_pneumothorax_code("J930")
    assert C.is_pneumothorax_code("J95811")
    assert not C.is_pneumothorax_code("J90")


# --- label concordance ----------------------------------------------------
def test_concordant_requires_agreement():
    chex = {"Pneumothorax": 1.0, "Edema": 1.0, "No Finding": 1.0}
    neg = {"Pneumothorax": 1.0, "Edema": -1.0}
    got = C.concordant(chex, neg)
    assert got == {"Pneumothorax": 1}            # edema discordant, no-finding absent in negbio


def test_bucket_priority_pneumothorax_wins():
    c = C.Candidate("s1", "10", labels={"Pneumothorax": 1, "Pleural Effusion": 1})
    assert C.bucket_of(c) == "pneumothorax"
    assert C.bucket_of(C.Candidate("s2", "10", labels={"Edema": 1})) == "effusion"
    assert C.bucket_of(C.Candidate("s3", "10", labels={"No Finding": 1})) == "normal"
    assert C.bucket_of(C.Candidate("s4", "10", labels={})) is None


# --- selection ------------------------------------------------------------
def _cand(sid, subj, labels=None, portable=False, priors=None):
    return C.Candidate(sid, subj, labels=labels or {}, portable=portable,
                       prior_study_ids=priors or [])


def test_select_fills_buckets_without_double_counting():
    cands = [
        _cand("s1", "10", {"Pneumothorax": 1}),
        _cand("s2", "11", {"Pleural Effusion": 1}),
        _cand("s3", "12", {"No Finding": 1}),
        _cand("s4", "13", {}, portable=True),
        _cand("s5", "14", {}, priors=["s0"]),
        _cand("s6", "15", {"Pneumothorax": 1}, portable=True),
    ]
    targets = {"normal": 1, "pneumothorax": 2, "effusion": 1, "priors": 1, "portable": 1}
    chosen = C.select(cands, targets)
    ids = {k: [c.study_id for c in v] for k, v in chosen.items()}
    assert ids["pneumothorax"] == ["s1", "s6"]   # s6 counted once, as pneumothorax
    assert ids["portable"] == ["s4"]
    assert ids["priors"] == ["s5"]
    all_ids = [i for v in ids.values() for i in v]
    assert len(all_ids) == len(set(all_ids))


def test_select_caps_studies_per_subject_except_priors():
    cands = [
        _cand("s1", "10", {"No Finding": 1}),
        _cand("s2", "10", {"No Finding": 1}),
        _cand("s3", "10", {"No Finding": 1}),   # third study of subject 10: capped out
        _cand("s4", "11", {"No Finding": 1}),
        _cand("s5", "10", {}, priors=["s1"]),   # priors bucket ignores the cap
    ]
    targets = {"normal": 3, "pneumothorax": 0, "effusion": 0, "priors": 1, "portable": 0}
    chosen = C.select(cands, targets, max_per_subject=2)
    assert [c.study_id for c in chosen["normal"]] == ["s1", "s2", "s4"]
    assert [c.study_id for c in chosen["priors"]] == ["s5"]


# --- manifest entry -------------------------------------------------------
def test_manifest_entry_reason_and_priority():
    c = C.Candidate("s7", "20", report_text=REPORT_OK, sections=C.parse_sections(REPORT_OK),
                    labels={"Pneumothorax": 1}, portable=True)
    entry = C.to_manifest_entry(c, diagnoses={"20": ["J95.811", "I10"]},
                                meds={"20": ["Warfarin 5mg"]},
                                labs={"20": [{"value": 1.2, "unit": "mg/dL", "date": "2100-01-01"}]},
                                genders={"20": "M"})
    assert entry["reason_codes"] == ["J95.811"]  # subject's own ptx code, not the fallback
    assert entry["priority"] == "stat"           # rule-out indication upgrades to stat
    assert entry["portable"] and entry["description"] == "CHEST (PORTABLE AP)"
    assert entry["meds"] == [{"display": "Warfarin 5mg"}]
    assert entry["labs"][0]["code"] == C.CREATININE_LOINC
    assert {"code": "J95.811"} in entry["problems"]
    assert entry["labels"]["gender"] == "M"


def test_manifest_entry_fallback_reason_code():
    c = C.Candidate("s8", "21", report_text=REPORT_OK, sections=C.parse_sections(REPORT_OK),
                    labels={"Pneumothorax": 1})
    entry = C.to_manifest_entry(c, {}, {}, {}, {})
    assert entry["reason_codes"] == ["J95.811"]  # no MIMIC-IV codes: heuristic fallback


def test_manifest_entry_positive_without_rule_out_is_urgent():
    text = "FINDINGS: Left pneumothorax.\nIMPRESSION: Left pneumothorax."
    c = C.Candidate("s9", "22", report_text=text, sections=C.parse_sections(text),
                    labels={"Pneumothorax": 1})
    entry = C.to_manifest_entry(c, {}, {}, {}, {})
    assert entry["priority"] == "urgent"
    assert "reason_codes" not in entry


# --- end to end on synthetic files ----------------------------------------
def test_cli_end_to_end(tmp_path):
    root = tmp_path / "cxr"
    (root / "files" / "p10" / "p1000001").mkdir(parents=True)
    (root / "files" / "p10" / "p1000002").mkdir(parents=True)
    (root / "files" / "p10" / "p1000001" / "s100.txt").write_text(REPORT_OK)
    (root / "files" / "p10" / "p1000002" / "s200.txt").write_text(
        "FINDINGS: Clear lungs.\n\nIMPRESSION: No acute process.")
    (root / "files" / "p10" / "p1000002" / "s201.txt").write_text(REPORT_NO_SECTIONS)
    (root / "cxr-study-list.csv").write_text(
        "subject_id,study_id,path\n1000001,100,x\n1000002,200,x\n1000002,201,x\n")
    (root / "chexpert.csv").write_text(
        "subject_id,study_id,Pneumothorax,No Finding\n1000001,100,1.0,\n1000002,200,,1.0\n"
        "1000002,201,,1.0\n")
    (root / "negbio.csv").write_text(
        "subject_id,study_id,Pneumothorax,No Finding\n1000001,100,1.0,\n1000002,200,,1.0\n"
        "1000002,201,,1.0\n")
    out = tmp_path / "cohort.json"
    rc = C.main(["--cxr-root", str(root), "--out", str(out),
                 "--normal", "1", "--pneumothorax", "1", "--effusion", "0",
                 "--priors", "0", "--portable", "0"])
    assert rc == 0
    data = json.loads(out.read_text())
    by_id = {s["study_id"]: s for s in data["studies"]}
    assert set(by_id) == {"s100", "s200"}        # s201 dropped: no FINDINGS/IMPRESSION
    assert by_id["s100"]["priority"] == "stat"
    assert by_id["s100"]["reason_codes"] == ["J95.811"]


def test_cli_refuses_manifest_inside_repo(tmp_path, capsys):
    import pytest
    root = tmp_path / "cxr"
    root.mkdir()
    inside = pathlib.Path(C._repo_root()) / "cohort.json"
    with pytest.raises(SystemExit):
        C.main(["--cxr-root", str(root), "--out", str(inside)])
