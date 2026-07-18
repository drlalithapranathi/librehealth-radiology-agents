"""Unit tests for the shared-cohort publish/pull tool (#68). Synthetic data, local temp dirs."""
import json
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import share_cohort as S  # noqa: E402


def _write_cohort(root: pathlib.Path):
    """A tiny curated tree: a 2-study manifest and one DICOM."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "showcase.json"
    manifest.write_text(json.dumps({"studies": [
        {"study_id": "s1", "subject_id": "10", "reason_codes": ["J95.811"], "meds": [{"display": "warfarin"}]},
        {"study_id": "s2", "subject_id": "11", "meds": []},
    ]}))
    dicom = root / "dl" / "files" / "p10" / "p10" / "s1"
    dicom.mkdir(parents=True)
    (dicom / "img.dcm").write_bytes(b"DICM-fake-pixels")
    return manifest, root / "dl"


def test_publish_then_pull_roundtrip(tmp_path):
    manifest, dicom_root = _write_cohort(tmp_path / "curated")
    share = tmp_path / "share"
    rc = S.main(["publish", "--manifest", str(manifest), "--dicom-root", str(dicom_root),
                 "--share-root", str(share), "--name", "v1"])
    assert rc == 0
    pub = share / "v1"
    assert (pub / "manifest.json").exists()
    assert (pub / "dicom" / "files" / "p10" / "p10" / "s1" / "img.dcm").exists()
    assert (pub / "SHA256SUMS").exists()
    prov = json.loads((pub / "SHARE.json").read_text())
    assert prov["studies"] == 2 and prov["with_reason_codes"] == 1 and prov["with_meds"] == 1

    dest = tmp_path / "dev"
    rc = S.main(["pull", "--share-root", str(share), "--name", "v1", "--dest", str(dest)])
    assert rc == 0
    local = dest / "cohort" / "v1"
    assert json.loads((local / "manifest.json").read_text())["studies"][0]["study_id"] == "s1"
    assert (local / "dicom" / "files" / "p10" / "p10" / "s1" / "img.dcm").read_bytes() == b"DICM-fake-pixels"


def test_cloud_mode_roundtrip(tmp_path):
    """--cloud drops POSIX metadata but copies the same content (contents-only rsync)."""
    manifest, dicom_root = _write_cohort(tmp_path / "curated")
    share = tmp_path / "share"
    assert S.main(["publish", "--manifest", str(manifest), "--dicom-root", str(dicom_root),
                   "--share-root", str(share), "--name", "v1", "--cloud"]) == 0
    dest = tmp_path / "dev"
    assert S.main(["pull", "--share-root", str(share), "--name", "v1", "--dest", str(dest),
                   "--cloud"]) == 0
    got = (dest / "cohort" / "v1" / "dicom" / "files" / "p10" / "p10" / "s1" / "img.dcm")
    assert got.read_bytes() == b"DICM-fake-pixels"


def test_pull_detects_corruption(tmp_path):
    manifest, dicom_root = _write_cohort(tmp_path / "curated")
    share = tmp_path / "share"
    S.main(["publish", "--manifest", str(manifest), "--dicom-root", str(dicom_root),
            "--share-root", str(share), "--name", "v1"])
    # tamper with a shared file AFTER the checksum manifest was written
    tampered = share / "v1" / "dicom" / "files" / "p10" / "p10" / "s1" / "img.dcm"
    tampered.write_bytes(b"corrupted")
    dest = tmp_path / "dev"
    rc = S.main(["pull", "--share-root", str(share), "--name", "v1", "--dest", str(dest)])
    assert rc == 1  # integrity mismatch is a nonzero exit


def test_publish_refuses_repo_destination(tmp_path):
    manifest, dicom_root = _write_cohort(tmp_path / "curated")
    inside = pathlib.Path(S._repo_root()) / "share"
    with pytest.raises(SystemExit):
        S.main(["publish", "--manifest", str(manifest), "--dicom-root", str(dicom_root),
                "--share-root", str(inside), "--name", "v1"])


def test_pull_refuses_repo_destination(tmp_path):
    manifest, dicom_root = _write_cohort(tmp_path / "curated")
    share = tmp_path / "share"
    S.main(["publish", "--manifest", str(manifest), "--dicom-root", str(dicom_root),
            "--share-root", str(share), "--name", "v1"])
    inside = pathlib.Path(S._repo_root()) / "devcopy"
    with pytest.raises(SystemExit):
        S.main(["pull", "--share-root", str(share), "--name", "v1", "--dest", str(inside)])


def test_missing_share_root_errors():
    with pytest.raises(SystemExit):
        S.main(["publish", "--manifest", "x.json", "--share-root", ""])
