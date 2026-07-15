"""Hermeticity for the interpretation-assistant suite.

The handler decides ONCE at import whether the pixel/model extras exist (`handler.PIXEL_TOOLING`).
On a dev machine (or any lane) that HAS torch installed, that flag is True, and the reason-code /
selection tests -- which feed a real `orthancStudyId` and expect the referral-rule STUBBED path --
would send `pneumothorax-detect` reaching for a LIVE Orthanc and get an ERROR (ConnectError) instead.

So default every test to the torch-free posture. The pixel-path tests (test_pneumothorax_detect.py)
opt back in explicitly with `handler.PIXEL_TOOLING = True` plus fakes for Orthanc and the model, so
they never touch the network either. Net: the suite behaves identically whether or not torch is
installed, which is the whole point of the import-time seam.
"""
import pytest

import handler


@pytest.fixture(autouse=True)
def _hermetic_pixel_tooling(monkeypatch):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False, raising=False)
