"""The real CXR model behind pneumothorax-detect (#71, slice of #27).

These run WITHOUT torch. The handler decides once at import whether the pixel/model extras exist
(handler.PIXEL_TOOLING) and reaches Orthanc and the model through module-level names, so a test can
substitute both. A seam that only exists when a 1.5GB dependency is installed is a seam CI never
exercises -- and the agent-tests lane installs neither extra, by design (conftest defaults it off).

What is guarded here is the HANDLER's contract around the model, not the model's accuracy:
  * a POSITIVE screen (Pneumothorax p >= threshold) becomes a COMPLETE finding with confidence and
    the instance it scored -- and that COMPLETE is what arms the pre-sign draft;
  * a NEGATIVE screen (p < threshold) reports STUBBED, NOT COMPLETE ("draft only on positives"), so
    a normal study never triggers a pre-sign chart write -- while still recording (evidenceRef +
    version) that the model DID run;
  * every way the model can fail to LOOK degrades to the referral rule / stub, never a fabricated
    negative; a model that threw is an honest ERROR;
  * toolsSelected[].version never claims a model that did not run.
"""
import pytest

import handler
from handler import handle

# reasonCode R91.8 does NOT match the pneumothorax referral rule (J93*/S270XXA/J95811), so a
# degrade falls through to a bare stub -- keeps the pixel-vs-stub tests unambiguous. The J93 variant
# is exercised explicitly below.
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
    "meta": {"traceId": "trc", "emittedAt": "2026-07-15T00:00:00Z", "source": "test"},
}


class _FakeOrthanc:
    """Stands in for a PACS. instances=[] models a study Orthanc has metadata for but no images."""

    def __init__(self, instances=("inst-frontal", "inst-lateral")):
        self._instances = list(instances)

    async def list_study_instances(self, study_id):
        return self._instances

    async def get_instance_dicom(self, instance_id):
        return instance_id.encode()  # bytes just carry which instance this is


def _pixels_on(monkeypatch, *, pneumothorax_p):
    """Turn the pixel path on with fakes for Orthanc, the decoder, and the model. The model returns
    a probability dict with the pneumothorax head set to `pneumothorax_p`."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score", lambda arr: {"Pneumothorax": pneumothorax_p, "Nodule": 0.10})


def _ptx(out):
    return next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")


def _selected(out, tool_id):
    return next(t for t in out["toolsSelected"] if t["toolId"] == tool_id)


async def test_a_positive_screen_becomes_a_complete_finding_with_confidence(monkeypatch):
    _pixels_on(monkeypatch, pneumothorax_p=0.87)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"
    assert f["confidence"] == 0.87
    assert "pneumothorax" in f["label"].lower()
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # the frontal, first in order
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"
    # a lone positive pixel finding makes the whole run PARTIAL (cxr-screen alongside stays STUBBED)
    assert out["overallStatus"] == "PARTIAL"


async def test_a_negative_screen_is_stubbed_not_complete_so_normals_stay_inert(monkeypatch):
    """THE #71 decision, mutation-checked. A negative screen must NOT emit COMPLETE: COMPLETE trips
    the unconditional pre-sign chart write in workflow.py, which would put "No acute findings" into
    every normal patient's chart ahead of the read. So a below-threshold pneumothorax reports
    STUBBED -- the model ran (evidenceRef + version prove it), it just offers no draft.

    Flip the `>=` branch in _pneumothorax_finding to always return COMPLETE and this fails."""
    _pixels_on(monkeypatch, pneumothorax_p=0.12)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "STUBBED"                      # NOT COMPLETE -> no pre-sign draft
    assert f["confidence"] is None
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # but the model DID run
    assert "negative" in f["label"].lower()
    # version still records the model ran, distinguishing it from a never-ran stub
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"
    # nothing COMPLETE anywhere -> the whole run is STUBBED, so _has_complete_finding stays False
    assert out["overallStatus"] == "STUBBED"


async def test_a_negative_screen_label_carries_no_critical_keyword_that_would_trip_a_flag(monkeypatch):
    """Belt-and-suspenders on the safety property. Even though STUBBED labels are not scanned by
    impression-generation today, the negative label is negation-worded so it stays correct once the
    scan becomes negation-aware (#78): it says "negative"/"no finding", never a bare critical claim."""
    _pixels_on(monkeypatch, pneumothorax_p=0.30)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    label = _ptx(out)["label"].lower()
    assert "negative" in label and "no finding" in label


async def test_the_model_result_supersedes_the_referral_reason_code(monkeypatch):
    """A study coded J93 (suspected pneumothorax) that the model scores NEGATIVE reports the model's
    STUBBED negative, NOT the referral-reason cross-check: the pixel read is the better signal, and
    falling back to "referral coded J93" after actually looking would be misleading."""
    _pixels_on(monkeypatch, pneumothorax_p=0.12)
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"   # model, not order.reasonCode=J93.1
    assert "negative" in f["label"].lower()


async def test_it_scores_the_first_instance_in_order_not_an_arbitrary_one(monkeypatch):
    """The frontal, not the lateral. list_study_instances guarantees (SeriesNumber, InstanceNumber)
    order; the handler must take the first scoreable instance and say which one it scored."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["evidenceRef"] == "orthanc:instance/inst-frontal"


async def test_without_the_model_extras_it_falls_back_to_the_referral_rule(monkeypatch):
    """The agent-tests CI lane (PIXEL_TOOLING False, the conftest default). A J93 study degrades to
    the referral-reason STUBBED cross-check, not a pixel result -- and must not claim a model."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", False)
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["status"] == "STUBBED"
    assert f["evidenceRef"] == "order.reasonCode=J93.1"
    assert _selected(out, "pneumothorax-detect")["version"] == "referral-rule-1"


async def test_a_study_with_no_instances_degrades_and_does_not_invent_a_negative(monkeypatch):
    """Orthanc has the study but no images. The tool could not look, so it must not say "nothing
    found" -- it falls through to the referral rule (here unmatched -> bare stub), never COMPLETE."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc(instances=[]))
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "STUBBED"
    assert f["label"] == ""          # NOT "no acute findings", NOT a model negative
    assert f["confidence"] is None


async def test_a_model_failure_is_an_honest_error_not_a_negative(monkeypatch):
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])

    def boom(arr):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(handler, "score", boom)
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "ERROR"
    assert f["confidence"] is None
    assert "RuntimeError" in f["label"]
    # the model reached a real instance before throwing, so the ERROR records which one -- and only
    # then does the audit attribute it to the model
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"
    assert _selected(out, "pneumothorax-detect")["version"] == "cxr-densenet121-res224-all"


async def test_an_orthanc_outage_degrades_and_is_not_attributed_to_the_model(monkeypatch):
    """A transport failure (Orthanc unreachable) is NOT a model failure: the model never ran. It
    must DEGRADE to the referral rule / stub, never ERROR, and the audit must NOT claim the model
    version -- claiming a model that never ran is the same lie as inventing a finding.

    Mutation: fold the fetch stage back under the model-stage try (so Orthanc errors become ERROR),
    or stamp the model version on status==ERROR, and this fails."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)

    class _DeadOrthanc:
        async def list_study_instances(self, study_id):
            raise ConnectionError("orthanc unreachable")

        async def get_instance_dicom(self, instance_id):  # pragma: no cover - never reached
            raise ConnectionError("orthanc unreachable")

    monkeypatch.setattr(handler, "OrthancClient", lambda: _DeadOrthanc())
    # J93 so the degrade lands on the referral-reason cross-check, proving it fell through
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J93.1"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    f = _ptx(out)
    assert f["status"] == "STUBBED"                      # degraded, NOT ERROR
    assert f["evidenceRef"] == "order.reasonCode=J93.1"  # fell through to the referral rule
    assert _selected(out, "pneumothorax-detect")["version"] == "referral-rule-1"  # NOT the model


async def test_a_model_missing_the_target_head_is_an_honest_error(monkeypatch):
    """If the loaded weights ever lack a Pneumothorax head, reading it is a KeyError -- which must
    surface as an honest ERROR, not a crash and not a silent miss."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)
    monkeypatch.setattr(handler, "OrthancClient", lambda: _FakeOrthanc())
    monkeypatch.setattr(handler, "dicom_to_greyscale", lambda b: [[0, 1], [2, 3]])
    monkeypatch.setattr(handler, "score", lambda arr: {"Nodule": 0.10})  # no Pneumothorax head
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    assert _ptx(out)["status"] == "ERROR"


async def test_skips_a_non_image_instance_and_scores_the_first_real_image(monkeypatch):
    """A non-image object -- a Structured Report, a radiation-dose SR, a presentation state -- can
    sort AHEAD of the frontal image. The tool must SKIP it and score the first real image, not abort
    the whole study on instances[0]."""
    monkeypatch.setattr(handler, "PIXEL_TOOLING", True)

    class _SRThenImage:
        async def list_study_instances(self, study_id):
            return ["inst-SR", "inst-frontal"]      # the SR sorts first

        async def get_instance_dicom(self, instance_id):
            return instance_id.encode()

    monkeypatch.setattr(handler, "OrthancClient", lambda: _SRThenImage())

    def decode(b):
        if b == b"inst-SR":
            raise handler.NotAnImage("Structured Report, no PixelData")
        return [[0, 1], [2, 3]]

    monkeypatch.setattr(handler, "dicom_to_greyscale", decode)
    monkeypatch.setattr(handler, "score", lambda arr: {"Pneumothorax": 0.91})
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    f = _ptx(out)
    assert f["status"] == "COMPLETE"                             # scored, not degraded
    assert f["evidenceRef"] == "orthanc:instance/inst-frontal"  # the frontal, not the SR


async def test_a_study_with_no_orthanc_id_degrades(monkeypatch):
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    ctx = {**CXR_CONTEXT, "study": {**CXR_CONTEXT["study"], "orthancStudyId": ""}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert _ptx(out)["status"] == "STUBBED"      # no study -> could not look -> not COMPLETE


async def test_the_model_never_runs_on_a_non_chest_study(monkeypatch):
    """The registry is the ONLY thing keeping non-CXRs away from a model that will confidently score
    anything. A head CT must not select pneumothorax-detect at all."""
    _pixels_on(monkeypatch, pneumothorax_p=0.90)
    ctx = {
        **CXR_CONTEXT,
        "study": {**CXR_CONTEXT["study"], "modality": "CT", "studyDescription": "CT HEAD W/O"},
    }
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    assert not any(f["toolId"] == "pneumothorax-detect" for f in out["findings"])
