"""Coverage for the REAL model behind cxr-screen: cxr_model.score / summarise / preprocessing.

Skipped unless torch + torchxrayvision are installed -- the agent-tests CI lane installs neither, so
this stays torch-free there, the same gate as handler.PIXEL_TOOLING. Where they ARE installed (a dev
machine, or a torch-enabled lane), this runs the actual DenseNet over a real chest film and pins the
model-card preprocessing that every other test stubs out. cxr_model.py had ZERO executed coverage
before this: the one real model in the system was never run under test.

Uses pydicom's bundled CR CHEST (RG1_UNCR.dcm); skipped if it cannot be fetched (one-time network).
"""
import numpy as np
import pytest

pytest.importorskip("torch")
pytest.importorskip("torchxrayvision")
pytest.importorskip("pydicom")

import cxr_model  # noqa: E402
from radagent_common.imaging import dicom_to_greyscale  # noqa: E402


@pytest.fixture(scope="module")
def real_cxr_pixels():
    from pydicom.data import get_testdata_file

    try:
        path = get_testdata_file("RG1_UNCR.dcm")
    except Exception:
        path = None
    if not path:
        pytest.skip("pydicom CR CHEST test film not available (needs one-time network fetch)")
    return dicom_to_greyscale(open(path, "rb").read())


def test_score_returns_a_bounded_probability_per_head(real_cxr_pixels):
    probs = cxr_model.score(real_cxr_pixels)
    assert isinstance(probs, dict) and len(probs) >= 14
    assert all(0.0 <= p <= 1.0 for p in probs.values())
    assert "Enlarged Cardiomediastinum" not in probs  # the suppressed head stays out


def test_scores_the_real_chest_film_positive_for_the_expected_heads(real_cxr_pixels):
    """This real CR CHEST reads positive for Mass / Lung Opacity / Nodule when decoded correctly.
    Pins the whole preprocessing chain (per-image normalise -> center-crop -> resize 224)."""
    probs = cxr_model.score(real_cxr_pixels)
    for head in ("Mass", "Lung Opacity", "Nodule"):
        assert probs[head] >= cxr_model.POSITIVE_THRESHOLD, f"{head}={probs[head]:.2f}"


def test_monochrome1_inversion_is_load_bearing(real_cxr_pixels):
    """The demo film is MONOCHROME1; imaging.dicom_to_greyscale inverts it. Scored WITHOUT that
    inversion the same positives collapse below threshold with NO error raised -- the exact silent
    failure the inversion prevents, and the reason the decode lives in the shared lib under test."""
    correct = cxr_model.score(real_cxr_pixels)
    uninverted = cxr_model.score(real_cxr_pixels.max() - real_cxr_pixels)  # undo the inversion
    for head in ("Nodule", "Lung Opacity", "Consolidation"):
        assert correct[head] >= cxr_model.POSITIVE_THRESHOLD
        assert uninverted[head] < cxr_model.POSITIVE_THRESHOLD


def test_summarise_reports_positives_with_the_top_confidence(real_cxr_pixels):
    probs = cxr_model.score(real_cxr_pixels)
    label, confidence = cxr_model.summarise(probs)
    assert "screening signal only" in label
    assert confidence == max(p for p in probs.values() if p >= cxr_model.POSITIVE_THRESHOLD)


def test_summarise_reports_an_honest_negative_and_no_confidence():
    """A NEGATIVE screen is reported honestly, not as a fabricated finding, and carries no
    confidence -- unlike the stub's constant fallback, this is a real model result."""
    label, confidence = cxr_model.summarise({"Nodule": 0.10, "Mass": 0.20})
    assert confidence is None
    assert "No finding" in label and "not a read" in label


def test_a_uniform_frame_refuses_rather_than_fabricating_a_negative():
    """A uniform frame carries no signal; score() must RAISE, not return zeros -- returning zeros
    would be a fabricated NEGATIVE, the #26 automation-bias trap. The handler turns this into ERROR."""
    with pytest.raises(ValueError):
        cxr_model.score(np.zeros((64, 64), dtype=np.float32))
