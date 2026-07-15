"""Unit tests for the MIMIC DICOM fix-up (#68). Synthetic Datasets only -- no MIMIC data (DUA)."""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pytest

pydicom = pytest.importorskip("pydicom", reason="ETL tooling needs pydicom")
from pydicom.dataset import Dataset  # noqa: E402

from dicom_fixup import (  # noqa: E402
    fixup_dataset, study_id_to_accession, DEFAULT_DESCRIPTION,
)

UID = "1.2.826.0.1.3680043.8.498.111"


def _ds(**kw) -> Dataset:
    ds = Dataset()
    ds.StudyInstanceUID = UID
    for k, v in kw.items():
        setattr(ds, k, v)
    return ds


def test_injects_accession_and_description_when_blank():
    ds = _ds()  # no AccessionNumber, no StudyDescription
    r = fixup_dataset(ds, "s56699142")
    assert ds.AccessionNumber == "s56699142"
    assert ds.StudyDescription == DEFAULT_DESCRIPTION
    assert ds.StudyInstanceUID == UID  # never touched
    assert r.accession == "s56699142" and r.description == DEFAULT_DESCRIPTION
    assert not r.warnings


def test_empty_string_accession_is_treated_as_blank():
    ds = _ds(AccessionNumber="", StudyDescription="")
    fixup_dataset(ds, "s1")
    assert ds.AccessionNumber == "s1"
    assert ds.StudyDescription == DEFAULT_DESCRIPTION


def test_existing_accession_is_kept_and_mismatch_warned():
    ds = _ds(AccessionNumber="REAL-ACC-9", StudyDescription="CHEST")
    r = fixup_dataset(ds, "s56699142")
    assert ds.AccessionNumber == "REAL-ACC-9"  # not clobbered
    assert ds.StudyDescription == "CHEST"
    assert r.accession == "REAL-ACC-9"
    assert any("kept it" in w for w in r.warnings)  # loader must key on the real value


def test_performed_procedure_step_description_used_before_default():
    ds = _ds(PerformedProcedureStepDescription="CHEST SINGLE VIEW")
    fixup_dataset(ds, "s2")
    assert ds.StudyDescription == "CHEST SINGLE VIEW"


def test_explicit_default_description_for_portable():
    ds = _ds()
    fixup_dataset(ds, "s3", default_description="CHEST (PORTABLE AP)")
    assert ds.StudyDescription == "CHEST (PORTABLE AP)"


def test_idempotent_second_pass_changes_nothing():
    ds = _ds()
    fixup_dataset(ds, "s4")
    r2 = fixup_dataset(ds, "s4")
    assert r2.changed == []


def test_blank_study_instance_uid_warns_but_does_not_fabricate():
    ds = Dataset()
    ds.StudyInstanceUID = ""
    r = fixup_dataset(ds, "s5")
    assert any("StudyInstanceUID" in w for w in r.warnings)
    assert ds.StudyInstanceUID == ""  # never fabricated


def test_accession_convention_is_the_study_id_verbatim():
    assert study_id_to_accession("s56699142") == "s56699142"
    assert study_id_to_accession("  s7 ") == "s7"
