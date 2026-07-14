"""The real CXR model behind cxr-screen (#27).

These run WITHOUT torch. The handler decides once at import whether the pixel/model extras exist
(handler.PIXEL_TOOLING) and reaches Orthanc and the model through module-level names, so a test can
substitute both. A seam that only exists when a 1.5GB dependency is installed is a seam CI never
exercises -- and the agent-tests lane installs neither extra, by design.

What is guarded here is the HANDLER's contract around the model, not the model's accuracy:
  * a real score becomes a COMPLETE finding with a confidence and the instance it scored;
  * every way the model can fail to run degrades to STUBBED or ERROR, and NEVER to a fabricated
    negative -- a tool that never looked must not report "nothing found";
  * toolsSelected[].version never claims a model that did not run.
"""
import pytest

import handler
from handler import handle

CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "orth-cxr-1",
        "modality": "CR",
        "studyDescription": "CHEST PA AND LATERAL",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc", "emittedAt": "2026-07-14T00:00:00Z", "source": "test"},
}


class _FakeOrthanc:
    """Stands in for a PACS. instances=[] models a study Orthanc has metadata for but no images."""

    def __init__(self, instances=("inst-frontal", "inst-lateral"), raises=None):
        self._instances = list(instances)
        self._raises = raises

    async def list_study_instances(self, study_id):
        return self._instances

    async def get_instance_dicom(self, instance_id):
        if self._raises:
            raise self._raises
        return b"DICM"


@pytest.fixture
def pixels_on(monkeypatch):
    """Turn the pixel path on with fakes standing in for Orthanc, the decoder, and the model."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score", lambda arr: {"Effusion": 0.87, "Nodule": 0.12})
    monkeypatch.setattr(
        handler, "summarise",
        lambda probs: ("Effusion p=0.87; screening signal only, not a read", 0.87),
    )


def _cxr(out):
    return next(f for f in out["findings"] if f["toolId"] == "cxr-screen")


def _selected(out, tool_id):
    return next(t for t in out["toolsSelected"] if t["toolId"] == tool_id)


async def test_a_real_score_becomes_a_complete_finding_with_confidence(pixels_on):
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _cxr(out)
    assert f["status"] == "COMPLETE"
    assert f["confidence"] == 0.87
    assert "Effusion" in f["label"]
    assert _selected(out, "cxr-screen")["version"] == "cxr-densenet121-res224-all"


async def test_it_scores_the_first_instance_in_order_not_an_arbitrary_one(pixels_on):
    """The frontal, not the lateral. list_study_instances guarantees (SeriesNumber, InstanceNumber)
    order; the handler must take instances[0] and say which one it scored."""
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _cxr(out)["evidenceRef"] == "orthanc:instance/inst-frontal"


async def test_without_the_model_extras_it_stays_stubbed_and_claims_no_model(monkeypatch):
    """The agent-tests CI lane. cxr-screen must degrade silently to a stub -- and must NOT advertise
    a model version it never ran."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _cxr(out)
    assert f["status"] == "STUBBED"
    assert f["confidence"] is None
    assert f["evidenceRef"] is None
    assert _selected(out, "cxr-screen")["version"] == "stub-0"


async def test_a_study_with_no_instances_stays_stubbed_and_does_not_invent_a_negative(monkeypatch):
    """Orthanc has the study but no images. The tool could not look, so it must not say "nothing
    found" -- that is the automation-bias trap the #26 COMPLETE-gate exists to prevent."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(instances=[]))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _cxr(out)
    assert f["status"] == "STUBBED"
    assert f["label"] == ""          # NOT "no acute findings"
    assert f["confidence"] is None


async def test_a_model_failure_is_an_honest_error_not_a_negative(monkeypatch):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])

    def boom(arr):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(handler, "score", boom)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _cxr(out)
    assert f["status"] == "ERROR"
    assert f["confidence"] is None
    assert "RuntimeError" in f["label"]
    # it DID attempt the model, so the version records which one
    assert _selected(out, "cxr-screen")["version"] == "cxr-densenet121-res224-all"


async def test_an_instance_without_pixels_stays_stubbed(monkeypatch):
    """A structured report sitting where the image should be. Skip, do not fail the study."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())

    def not_an_image(b):
        raise handler.NotAnImage("no PixelData")

    monkeypatch.setattr(handler, "dicom_to_greyscale", not_an_image)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _cxr(out)["status"] == "STUBBED"


async def test_a_study_with_no_orthanc_id_stays_stubbed(pixels_on):
    ctx = {**CXR_CONTEXT, "study": {**CXR_CONTEXT["study"], "orthancStudyId": ""}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert _cxr(out)["status"] == "STUBBED"


async def test_the_model_never_runs_on_a_non_chest_study(pixels_on):
    """The registry is the ONLY thing keeping non-CXRs away from a model that will confidently score
    anything (it returns Lung Opacity p=0.996 on pure noise). A head CT must not reach it."""
    ctx = {
        **CXR_CONTEXT,
        "study": {**CXR_CONTEXT["study"], "modality": "CT", "studyDescription": "CT HEAD W/O"},
    }
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert not any(f["toolId"] == "cxr-screen" for f in out["findings"])
    assert all(f["status"] == "STUBBED" for f in out["findings"])
