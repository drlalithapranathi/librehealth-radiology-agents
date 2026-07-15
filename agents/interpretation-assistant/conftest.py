import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _pixel_tooling_off_by_default(monkeypatch):
    """Default the pixel path OFF in every test, so a handler test never reaches a live Orthanc.

    cxr-screen is only active when torch + the imaging extra are installed (handler.PIXEL_TOOLING).
    With them present -- a normal dev machine, and the deployed agent image -- a CR/chest handler
    test would otherwise build a real OrthancClient and make a live HTTP call: the referral-reason
    tests passed only on the torch-free CI lane and hit the network everywhere else. Force it off by
    default (the CI-lane reality). Tests that exercise the pixel path opt back in explicitly -- see
    tests/test_cxr_screen.py's ``pixels_on`` (which also stubs Orthanc) and tests/test_cxr_model.py.
    """
    import handler
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False)
