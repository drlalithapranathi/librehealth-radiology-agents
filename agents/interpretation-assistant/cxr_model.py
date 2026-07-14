"""The first REAL model behind the tool registry: chest-X-ray screening (#27).

Every other tool in the registry is still STUBBED. This one actually reads pixels and runs a
pretrained classifier -- TorchXRayVision's DenseNet-121 (Cohen et al.), trained on the pooled public
CXR corpora (NIH ChestX-ray14, PadChest, CheXpert, MIMIC-CXR, OpenI, Kaggle). 18 pathology heads,
CPU inference in well under a second.

WHAT THIS IS NOT. It is a SCREENING signal, not a diagnosis, and it is not a substitute for the
read. Two properties make that more than boilerplate:

  * It scores ANYTHING. Handed uniform noise it returns "Lung Opacity: 0.996" -- measured, not
    hypothetical. There is no in-distribution check; a scout view, a lateral, a rotated film, or a
    non-chest study that slipped past the registry all get a confident number back. The registry
    selecting on modality+region is the ONLY thing keeping non-CXRs away from it.
  * The training corpora are labelled by NLP over report text, so the labels are noisy and the
    operating points are not calibrated for any particular population.

Which is why a COMPLETE finding from this tool does NOT authorise a chart write. See
orchestrator/workflow.py: the pre-sign fhir2 draft has its own explicit switch, deliberately
independent of tool status, so "the AI ran" and "we wrote to the patient's chart" stay two separate
decisions (#27/#30).

Weights are baked into the image at build time (see Dockerfile) -- an agent that reaches for the
network mid-study to download a model is an agent that fails mid-study.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

# EAGER, not lazy. This module's importability IS the "are the model extras installed?" signal the
# handler branches on: no torch -> ImportError here -> the handler leaves cxr-screen STUBBED, and
# the agent-tests CI lane (which installs neither torch nor the imaging extra) stays green.
# Importing torch lazily inside score() would make this module import fine everywhere, the handler
# would think the model was available, and it would go to the network mid-study to find out
# otherwise. Model WEIGHTS still load lazily (_load) -- that is the expensive part, not the import.
import torch  # noqa: F401  (re-exported failure signal; used in score())
import torchxrayvision as xrv

log = logging.getLogger(__name__)

MODEL_WEIGHTS = "densenet121-res224-all"

# A pathology is REPORTED when its sigmoid output clears this. 0.5 is the model's own nominal
# operating point; it is not tuned to a population and is deliberately a named constant rather than
# a magic number, because tuning it is a clinical decision that needs data we do not have (the same
# argument as #64's registry corpus).
POSITIVE_THRESHOLD = 0.5

# Pathologies the model exposes but which are not screening-actionable on a plain film, or which
# duplicate another head. Dropped from the reported set so the label stays readable.
_SUPPRESSED = frozenset({"Enlarged Cardiomediastinum"})

_model = None
_lock = threading.Lock()


def _load():
    """Load the WEIGHTS lazily, once, thread-safe.

    Deferred because it is the expensive part (~30MB off disk), not because of the import: a worker
    that never sees a chest film should never pay for it. Two concurrent studies must not each build
    a DenseNet, hence the double-checked lock.
    """
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                m = xrv.models.DenseNet(weights=MODEL_WEIGHTS)
                m.eval()
                _model = m
                log.info("cxr-screen: loaded %s (%d heads)", MODEL_WEIGHTS, len(m.pathologies))
    return _model


def score(greyscale) -> dict[str, float]:
    """Score one 2-D greyscale array (MONOCHROME2 convention, from radagent_common.imaging).

    Returns {pathology: probability} for every head. Preprocessing follows the model card:
    per-image normalisation to [-1024, 1024], centre crop, resize to 224.

    Per-image min/max normalisation (rather than a fixed bit-depth maxval) is deliberate: after
    rescale slope/intercept the array is in real units whose range depends on modality and
    manufacturer, and the DICOM bit depth no longer describes it. Normalising to the image's own
    range is what the model card does for DICOM input and keeps contrast handling consistent across
    a 12-bit CR and a rescaled DX.
    """
    import numpy as np

    model = _load()

    arr = np.asarray(greyscale, dtype=np.float32)
    arr = arr - arr.min()
    peak = float(arr.max())
    if peak <= 0:
        # A uniform frame carries no signal. Returning zeros here would be a fabricated NEGATIVE --
        # the exact trap the #26 COMPLETE-gate exists to prevent -- so refuse instead and let the
        # caller mark the tool ERROR.
        raise ValueError("image is uniform (max == min); nothing to score")
    arr = xrv.datasets.normalize(arr, peak)          # -> [-1024, 1024]

    arr = arr[None, ...]                              # add channel
    arr = xrv.datasets.XRayCenterCrop()(arr)
    arr = xrv.datasets.XRayResizer(224)(arr)

    with torch.no_grad():
        out = model(torch.from_numpy(arr)[None, ...])[0]

    return {
        name: float(p)
        for name, p in zip(model.pathologies, out)
        if name and name not in _SUPPRESSED
    }


def summarise(probs: dict[str, float], threshold: float = POSITIVE_THRESHOLD) -> tuple[str, Optional[float]]:
    """Turn 18 probabilities into the contract's (label, confidence) pair.

    A NEGATIVE screen is reported honestly ("no finding above threshold") and is a genuine model
    result -- unlike the stub's constant fallback, which asserted a negative no model had ever
    looked for. That distinction is the whole reason this tool may report COMPLETE at all. It still
    does not authorise a chart write; that is gated separately.
    """
    positives = sorted(
        ((n, p) for n, p in probs.items() if p >= threshold),
        key=lambda kv: -kv[1],
    )
    if not positives:
        top = max(probs.items(), key=lambda kv: kv[1], default=(None, 0.0))
        return (
            f"No finding at or above p={threshold:g} "
            f"(highest: {top[0]} p={top[1]:.2f}); screening signal only, not a read",
            None,
        )
    shown = ", ".join(f"{n} p={p:.2f}" for n, p in positives[:4])
    return (f"{shown}; screening signal only, not a read", positives[0][1])
