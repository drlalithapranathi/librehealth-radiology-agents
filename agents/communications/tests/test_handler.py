"""comms.dispatch contract test + urgency-routing assertions (#17)."""
from handler import handle
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_routine_dispatch_is_contract_valid_and_sent():
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("comms.dispatch", out)
    assert out["workflowId"] == "wf_test"
    assert out["dispatchStatus"] == "SENT"
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox"]


async def test_critical_result_also_pages_oncall():
    out = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"criticalFlags": [{"label": "aortic dissection", "severity": "critical"}]},
    })
    validate_skill_output("comms.dispatch", out)
    channels = [c["channel"] for c in out["channelResults"]]
    assert channels == ["ehr-inbox", "oncall-pager"]
    assert all(c["status"] == "SENT" for c in out["channelResults"])


async def test_failed_verification_also_pages_oncall():
    out = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "verification": {"verificationStatus": "FAIL"},
    })
    validate_skill_output("comms.dispatch", out)
    assert "oncall-pager" in [c["channel"] for c in out["channelResults"]]


async def test_escalation_rung_dispatches_its_requested_channels():
    """A fired sign-off ladder rung (#29) dispatches exactly the channels the policy chose."""
    out = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "escalation": {"level": 2, "targetRole": "on-call-radiologist",
                       "channels": ["pager", "sms"], "urgency": "critical", "attempt": 1,
                       "reason": "sign-off gate timed out awaiting radiologist"},
    })
    validate_skill_output("comms.dispatch", out)
    assert [c["channel"] for c in out["channelResults"]] == ["pager", "sms"]
    assert all(c["status"] == "SENT" for c in out["channelResults"])


async def test_unexpected_skill_raises():
    import pytest
    with pytest.raises(ValueError):
        await handle("comms.sendCarrierPigeon", {"studyContext": SAMPLE_CONTEXT})
