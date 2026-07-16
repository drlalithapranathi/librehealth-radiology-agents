"""Hermeticity for the interpretation-assistant suite.

The handler decides ONCE at import whether the pixel/model extras exist (`handler.PIXEL_TOOLING`).
On a dev machine (or any lane) that HAS torch installed, that flag is True, and the reason-code /
selection tests -- which feed a real `orthancStudyId` -- would send `pneumothorax-detect` reaching
for a LIVE Orthanc. The fetch-stage guard catches the ConnectError and DEGRADES to the referral
rule, so those tests would still pass -- but only by timing out against the network first, and
only for as long as nothing on localhost:8042 answers. A suite whose green depends on the network
being ABSENT is not hermetic.

So default every test to the torch-free posture. The pixel-path tests (test_pneumothorax_detect.py)
opt back in explicitly with `handler.PIXEL_TOOLING = True` plus fakes for Orthanc and the model, so
they never touch the network either. Net: the suite behaves identically -- and at the same speed --
whether or not torch is installed, which is the whole point of the import-time seam.
"""
import pytest

import handler


@pytest.fixture(autouse=True)
def _hermetic_pixel_tooling(monkeypatch):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False, raising=False)
