"""DICOM decode guards (#27).

Every test here builds a real DICOM in memory rather than mocking pydicom: the bugs this module
exists to prevent (a MONOCHROME1 negative, an unrescaled CT) live in the *interpretation of the
header*, and a mock that returns whatever the test says defeats the entire point.
"""
import io

import pytest

pydicom = pytest.importorskip("pydicom")
np = pytest.importorskip("numpy")

from pydicom.dataset import Dataset, FileMetaDataset  # noqa: E402
from pydicom.uid import ExplicitVRLittleEndian, generate_uid  # noqa: E402

from radagent_common.imaging import NotAnImage, dicom_to_greyscale  # noqa: E402


def _dicom(pixels, *, photometric="MONOCHROME2", slope=None, intercept=None,
           has_pixels=True) -> bytes:
    arr = np.asarray(pixels, dtype=np.uint16)
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.1"  # CR image storage
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.1"
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality = "CR"
    ds.PhotometricInterpretation = photometric
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    if slope is not None:
        ds.RescaleSlope = slope
    if intercept is not None:
        ds.RescaleIntercept = intercept
    if has_pixels:
        ds.Rows, ds.Columns = arr.shape
        ds.PixelData = arr.tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


PIXELS = [[0, 100], [200, 300]]


def test_monochrome2_passes_through_unchanged():
    out = dicom_to_greyscale(_dicom(PIXELS))
    assert out.tolist() == [[0.0, 100.0], [200.0, 300.0]]


def test_monochrome1_is_inverted_so_dense_reads_bright():
    """THE bug this module exists for. In MONOCHROME1 low values are WHITE, so handing the raw
    array to a MONOCHROME2-trained model feeds it a photographic negative of the X-ray. Nothing
    raises -- it is a plausible-looking image -- and the model returns a confident wrong answer.

    Mutation-checked: delete the inversion branch in imaging.py and this test fails while every
    other test in this file still passes."""
    out = dicom_to_greyscale(_dicom(PIXELS, photometric="MONOCHROME1"))
    # max (300) - value: the darkest stored value becomes the brightest, and vice versa.
    assert out.tolist() == [[300.0, 200.0], [100.0, 0.0]]
    # and it is genuinely NOT the passthrough
    assert out.tolist() != [[0.0, 100.0], [200.0, 300.0]]


def test_rescale_slope_and_intercept_are_applied():
    """Identity for most CR/DX (slope 1 / intercept 0) and the whole ballgame for CT, where it is
    the difference between raw stored values and Hounsfield units."""
    out = dicom_to_greyscale(_dicom(PIXELS, slope=2.0, intercept=-1000.0))
    assert out.tolist() == [[-1000.0, -800.0], [-600.0, -400.0]]


def test_malformed_rescale_falls_back_to_identity_rather_than_failing_the_study():
    """A scanner CAN put junk in a DS field. pydicom's setter refuses to construct one, so the bad
    file is authored by patching the encoded bytes -- which is exactly how it arrives off the wire.

    Verified that this reaches the guard rather than testing nothing: on READ pydicom hands the
    malformed DS back as a plain `str`, and float() on it raises ValueError. So `_num`'s fallback is
    the thing standing between a junk slope and a dead study, and this is the test that proves it.
    """
    good = _dicom(PIXELS, slope="12345678")           # 8 chars, a valid DS
    bad = good.replace(b"12345678", b"abcdefgh")      # same length, no longer a number
    assert bad != good, "byte patch did not apply -- the fixture is not malformed"

    out = dicom_to_greyscale(bad)
    assert out.tolist() == [[0.0, 100.0], [200.0, 300.0]]


def test_instance_without_pixel_data_raises_not_an_image():
    """A structured report or presentation state sitting in the same study must be SKIPPABLE, not
    fatal -- a tool that dies because a study contains an SR is a tool that never runs."""
    with pytest.raises(NotAnImage):
        dicom_to_greyscale(_dicom(PIXELS, has_pixels=False))


def test_result_is_2d_float_regardless_of_stored_integer_type():
    out = dicom_to_greyscale(_dicom(PIXELS))
    assert out.ndim == 2
    assert out.dtype == np.float32


def test_multiframe_colour_is_refused_as_a_clip_not_scored():
    """A multi-frame colour object is (frames, H, W, 3) -- an ultrasound/fluoro cine or video SC.
    It used to break the 2-D contract (frame 0 still colour), then briefly got silently SCORED
    (frame first, colour collapse) -- a confident number about the wrong kind of pixels. Policy:
    refuse with NotAnImage, so the caller skips the clip and keeps walking to the real image (a
    clip sorted ahead of the frontal no longer swallows the screen)."""
    frame0 = np.array([[[255, 0, 0], [0, 255, 0]],
                       [[0, 0, 255], [255, 255, 255]]], dtype=np.uint8)
    arr = np.stack([frame0, np.zeros_like(frame0)])   # two frames: the shape that makes it a clip

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7.4"  # multi-frame colour SC
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.7.4"
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.Modality = "OT"
    ds.PhotometricInterpretation = "RGB"
    ds.SamplesPerPixel = 3
    ds.PlanarConfiguration = 0
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = 2
    ds.Rows, ds.Columns = 2, 2
    ds.PixelData = arr.tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)

    with pytest.raises(NotAnImage, match="clip"):
        dicom_to_greyscale(buf.getvalue())
