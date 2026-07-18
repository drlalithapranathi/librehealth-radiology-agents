"""Unit tests for the MIMIC ETL pure logic (#68). No live stack, no MIMIC data."""
import sys
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import manifest as M  # noqa: E402
import fetch  # noqa: E402
from dicom_fixup import PORTABLE_DESCRIPTION  # noqa: E402

SAMPLE = str(HERE.parent / "sample_cohort.json")


# --- manifest ------------------------------------------------------------
def test_manifest_parses_sample():
    studies = M.load_manifest(SAMPLE)
    assert len(studies) == 3
    s = {x.study_id: x for x in studies}
    assert s["s90000001"].subject_id == "19000001"
    assert isinstance(s["s90000002"].problems[0], M.Problem)
    assert s["s90000002"].problems[0].code == "J95.811"
    assert isinstance(s["s90000001"].labs[0], M.Lab)
    assert s["s90000002"].meds[0].display == "warfarin"


def test_portable_study_description():
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    assert studies["s90000002"].study_description == PORTABLE_DESCRIPTION  # portable -> AP
    assert studies["s90000001"].study_description == "CHEST (PA AND LAT)"


# --- fetch key derivation (no network) -----------------------------------
def test_fetch_prefix_derivation():
    s = M.CohortStudy(study_id="s56699142", subject_id="10000032")
    assert fetch.study_prefix(s) == "files/p10/p10000032/s56699142/"


def test_fetch_prefix_strips_optional_prefixes():
    s = M.CohortStudy(study_id="56699142", subject_id="p10000032")
    assert fetch.study_prefix(s) == "files/p10/p10000032/s56699142/"


# --- load_cohort orchestration with a fake client ------------------------
class _FakeClient:
    def __init__(self):
        self.calls = []

    def create_patient(self, subject_id, gender="U"):
        self.calls.append(("patient", subject_id)); return f"pat-{subject_id}"

    def create_encounter(self, patient, when):
        self.calls.append(("encounter", patient)); return f"enc-{patient}"

    def insert_radiology_order(self, patient, enc, accession, concept, priority="routine",
                               reason_concept_uuid=None):
        self.calls.append(("order", accession, priority, reason_concept_uuid))
        return f"ord-{accession}"

    def ensure_order_reason(self, codes, display=""):
        self.calls.append(("reason", tuple(codes), display)); return "reason-" + "+".join(codes)

    def ensure_drug(self, name):
        self.calls.append(("drug", name)); return f"drug-{name}"

    def insert_drug_order(self, patient, enc, drug):
        self.calls.append(("drugorder", drug)); return f"rx-{drug}"

    def create_observation(self, patient, code, value, unit, when):
        self.calls.append(("obs", code)); return "obs-x"

    def create_condition(self, patient, code, when):
        self.calls.append(("cond", code)); return "cond-x"

    def seed_diagnostic_report(self, patient, order, concept, text, status="preliminary"):
        self.calls.append(("report", order, status)); return "rep-x"


def test_load_study_wires_the_join_and_ehr():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["accession"] == "s90000002"          # study_id verbatim
    assert r["order"] == "ord-s90000002"
    assert r["report"] == "rep-x"
    assert r["ehr"]["problems"] == 1              # J95.811 loaded
    assert r["ehr"]["meds"] == 1                  # warfarin loads as a presence-only drug order
    # the order carries the stat priority (drives triage URGENT downstream)
    assert ("order", "s90000002", "stat", "reason-J95.811") in c.calls
    # a preliminary report is seeded basedOn the order
    assert ("report", "ord-s90000002", "preliminary") in c.calls


def test_load_study_ehr_is_best_effort():
    import load_cohort

    class _BadEhr(_FakeClient):
        def create_condition(self, *a, **k):
            raise RuntimeError("no concept mapping")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadEhr()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # the study still loads (order + report) despite the EHR failure
    assert r["order"] == "ord-s90000002" and r["report"] == "rep-x"
    assert any("problem" in w for w in r.get("warnings", []))


def test_order_reason_reaches_the_order_with_problem_display():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # the matching problem's display names the reason concept
    assert ("reason", ("J95.811",), "Postprocedural pneumothorax") in c.calls


def test_reason_failure_never_costs_the_order():
    import load_cohort

    class _BadReason(_FakeClient):
        def ensure_order_reason(self, *a, **k):
            raise RuntimeError("no ICD-10 source")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadReason()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    # order still created, reason-less, with the warning recorded (mirrors the #81 resolver)
    assert r["order"] == "ord-s90000002"
    assert ("order", "s90000002", "stat", None) in c.calls
    assert any("order reason" in w for w in r.get("warnings", []))


def test_a_study_without_reason_codes_never_calls_ensure_order_reason():
    import load_cohort
    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _FakeClient()
    load_cohort.load_study(c, studies["s90000001"], concept_uuid="concept-uuid")
    assert not any(call[0] == "reason" for call in c.calls)
    assert ("order", "s90000001", "routine", None) in c.calls


def test_med_failure_is_best_effort():
    import load_cohort

    class _BadMeds(_FakeClient):
        def ensure_drug(self, *a, **k):
            raise RuntimeError("drug order path refused")

    studies = {x.study_id: x for x in M.load_manifest(SAMPLE)}
    c = _BadMeds()
    r = load_cohort.load_study(c, studies["s90000002"], concept_uuid="concept-uuid")
    assert r["order"] == "ord-s90000002" and r["report"] == "rep-x"
    assert r["ehr"]["meds"] == 0
    assert any("warfarin" in w for w in r.get("warnings", []))


# --- stable dictionary UUIDs (no DB) --------------------------------------
def test_reason_concept_uuid_is_stable_and_order_insensitive():
    import bootstrap_radiology_concept as B
    a = B.reason_concept_uuid(["J93.0", "J95.811"])
    b = B.reason_concept_uuid(["J95.811", "J93.0"])
    assert a == b                                  # sorted seed: manifest order never forks the concept
    assert a != B.reason_concept_uuid(["J95.811"])  # different code set, different concept


def test_drug_uuids_normalise_name():
    import bootstrap_radiology_concept as B
    assert B.drug_uuid(" Warfarin ") == B.drug_uuid("warfarin")
    assert B.drug_concept_uuid("warfarin") != B.drug_uuid("warfarin")
