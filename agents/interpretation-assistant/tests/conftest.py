"""Hermeticity for the interpretation-assistant suite.

The handler decides ONCE at import whether the pixel/model extras exist (`handler.PIXEL_TOOLING`).
On a dev machine (or any lane) that HAS torch installed, that flag is True, and the reason-code /
selection tests -- which feed a real `orthancStudyId` -- would send `pneumothorax-detect` reaching
for a LIVE Orthanc. The fetch-stage guard catches the ConnectError and DEGRADES to the referral
rule, so those tests would still pass -- but their green would depend on the connect FAILING
(the default base URL is http://orthanc:8042, a hostname that resolves nowhere off-compose, so
the failure is an instant DNS-resolution ConnectError, not a timeout) and it could flip if
ORTHANC_BASE_URL points at a live Orthanc that can serve the referenced study (an answering
host alone changes the failure mode; a served study changes the RESULT). A suite whose green
depends on how the network fails is not hermetic.

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
