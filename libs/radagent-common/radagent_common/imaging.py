"""DICOM pixel decoding for the CAD tools (#27). Optional extra: `radagent-common[imaging]`.

WHY THIS IS A SHARED-LIB MODULE, not a helper inside one agent: every tool that reads pixels must
read them the SAME way. A model trained on rescaled, correctly-oriented Hounsfield/greyscale data
and fed a raw MONOCHROME1 array will score a photographic negative and return a confident, wrong
answer -- silently, with no error anywhere. That failure mode does not belong in six copies.

WHY IT IS AN OPTIONAL EXTRA: pydicom + numpy are a real dependency weight, and only the agents that
actually run a model need them (same reasoning as [a2a] and [otel]). `import radagent_common.imaging`
without the extra raises a clear ImportError naming the extra, rather than a bare ModuleNotFoundError
on pydicom from three frames deep.

PHI: the arrays this module returns ARE the patient's imaging data, and the DICOM header it parses
carries their identity. Nothing here logs, caches, or persists either. Callers score and discard;
only the derived finding travels (golden rule 2).
"""
from __future__ import annotations

import io
from typing import Any

try:
    import numpy as np
    import pydicom
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the import-guard test
    raise ImportError(
        "radagent_common.imaging needs the imaging extra: pip install 'radagent-common[imaging]' "
        "(pydicom + numpy). Only agents that read pixels require it."
    ) from exc


class NotAnImage(ValueError):
    """The instance carries no pixel data we can score (e.g. a structured report or presentation
    state sitting in the same study). Callers SKIP such an instance rather than fail the study --
    a tool that errors out because a study happens to contain an SR is a tool that never runs."""


def dicom_to_greyscale(dicom_bytes: bytes) -> "np.ndarray":
    """Decode one DICOM instance to a 2-D float array in a consistent greyscale convention.

    Returns HIGH = DENSE/BRIGHT (the MONOCHROME2 convention), which is what every CXR model in the
    wild is trained on. The three corrections applied, in order, are the three that quietly ruin a
    CAD result if you skip them:

      1. RescaleSlope / RescaleIntercept -> stored values become real values. Skipping it is
         usually harmless for CR/DX (slope 1, intercept 0) and catastrophic for CT (where it is the
         difference between raw stored values and Hounsfield units).
      2. PhotometricInterpretation == MONOCHROME1 -> INVERT. In MONOCHROME1, low values are white.
         Feeding it to a MONOCHROME2-trained model hands it a negative of the X-ray. It looks like
         a plausible image, so nothing raises -- the model just quietly scores the wrong thing.
         This is the single most common way a CXR pipeline is silently wrong.
      3. Multi-frame instances -> take the first frame. A single-frame CXR is the common case; a
         caller that needs a specific frame of a cine loop should not be using this function.

    Windowing (WindowCenter/WindowWidth) is deliberately NOT applied: it is a viewing preference
    baked in by the modality/technologist, and models normalize their own input. Applying it would
    throw away dynamic range the model wants.
    """
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes), force=True)

    if "PixelData" not in ds:
        raise NotAnImage(
            f"instance has no PixelData (SOPClassUID={getattr(ds, 'SOPClassUID', 'unknown')})"
        )

    arr = ds.pixel_array  # may raise if the transfer syntax needs a codec we lack -- let it
    arr = np.asarray(arr)

    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        # Colour (e.g. an RGB secondary capture). Luminance, so a screenshot in the study does not
        # crash the tool -- ITU-R BT.601, the same weights DICOM uses for YBR conversion.
        arr = arr[..., :3] @ np.array([0.299, 0.587, 0.114])
    elif arr.ndim > 2:
        arr = arr[0]  # multi-frame -> first frame

    arr = arr.astype(np.float32)

    slope = _num(getattr(ds, "RescaleSlope", 1.0), 1.0)
    intercept = _num(getattr(ds, "RescaleIntercept", 0.0), 0.0)
    if slope != 1.0 or intercept != 0.0:
        arr = arr * slope + intercept

    if str(getattr(ds, "PhotometricInterpretation", "MONOCHROME2")).upper() == "MONOCHROME1":
        arr = arr.max() - arr

    return arr


def _num(v: Any, default: float) -> float:
    """DICOM DS values arrive as pydicom DSfloat / str / None. A malformed slope must not take the
    whole study down -- fall back to the identity transform."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
