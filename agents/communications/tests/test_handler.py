"""comms skill contract tests: #17 dispatch channel-routing + #52 checkAck/escalate split."""
import json

from jsonschema import Draft202012Validator

from handler import handle
from radagent_common import paths
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

ESCALATION_RUNG = {
    "level": 2, "targetRole": "on-call-radiologist", "channels": ["pager", "sms"],
    "urgency": "critical", "attempt": 1,
    "reason": "sign-off gate timed out awaiting radiologist",
}


def _input_validator(skill_id: str) -> Draft202012Validator:
    """The skill's $defs/input schema. Nothing validates skill INPUTS in the pipeline yet -- these
    tests are what keep the input schemas honest until it does."""
    schema = json.loads(paths.skill_schema(skill_id).read_text())
    return Draft202012Validator(schema["$defs"]["input"])


def test_dispatch_input_schema_admits_the_payloads_the_orchestrator_sends():
    """The input schema is additionalProperties:false, so every key the orchestrator actually
    sends must be declared -- including the #29 `escalation` slice that escalate_activity passes
    and _dispatch() reads. Omitting it rejects every escalation page the moment inputs are
    validated (authoritative rung shape: escalation-policy.schema.json $defs/dispatchEscalation)."""
    v = _input_validator("comms.dispatch")
    v.validate({"studyContext": SAMPLE_CONTEXT})                       # routine COMMUNICATE hand-off
    v.validate({"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG})   # a fired rung
    v.validate({                                                        # the full pass-forward set
        "studyContext": SAMPLE_CONTEXT,
        "report": {"diagnosticReportId": "DiagnosticReport/1"},
        "impression": {"criticalFlags": []},
        "verification": {"verificationStatus": "PASS"},
    })


# --- comms.dispatch (channel routing, #17) ------------------------------------------
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
    payload = {"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG}
    _input_validator("comms.dispatch").validate(payload)   # the wire shape holds...
    out = await handle("comms.dispatch", payload)          # ...and the handler honours it
    validate_skill_output("comms.dispatch", out)
    assert [c["channel"] for c in out["channelResults"]] == ["pager", "sms"]
    assert all(c["status"] == "SENT" for c in out["channelResults"])


# --- comms.checkAck (#52) -----------------------------------------------------------
async def test_check_ack_is_contract_valid():
    out = await handle("comms.checkAck", {"studyContext": SAMPLE_CONTEXT, "taskId": "Task/task-1"})
    validate_skill_output("comms.checkAck", out)
    assert out["taskId"] == "Task/task-1"
    assert out["ackStatus"] == "REQUESTED"
    assert out["overdue"] is False


# --- comms.escalate (#52) -----------------------------------------------------------
async def test_escalate_is_contract_valid_and_stub_has_no_target():
    out = await handle("comms.escalate", {"studyContext": SAMPLE_CONTEXT, "taskId": "Task/task-1"})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is False
    assert out["reason"]


async def test_unexpected_skill_raises():
    import pytest
    with pytest.raises(ValueError):
        await handle("comms.sendCarrierPigeon", {"studyContext": SAMPLE_CONTEXT})
