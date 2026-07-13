"""comms skills: #17 channel routing, #29's rung pass-through, and the real #52 closed loop.

The closed loop is the whole point of this agent: a critical finding produces a Communication (we
told someone) and a Task (did they acknowledge), and an unanswered Task escalates to on-call. These
drive it end to end against in-memory ledger/fhir2 doubles, so the state that ends up in the ledger
is what is asserted -- not just the shape of the reply.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from jsonschema import Draft202012Validator

import handler
from handler import handle
from radagent_common import paths
from radagent_common.fhir_models import TaskStatus
from radagent_common.validation import validate_skill_output

from fakes import FakeFhir2, FakeLedger

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"fhirServiceRequestId": "ServiceRequest/sr-1"},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

ESCALATION_RUNG = {
    "level": 2, "targetRole": "on-call-radiologist", "channels": ["pager", "sms"],
    "urgency": "critical", "attempt": 1,
    "reason": "sign-off gate timed out awaiting radiologist",
}

CRITICAL = {"criticalFlags": [{"label": "aortic dissection", "severity": "critical"}]}


@pytest.fixture(autouse=True)
def stores():
    """Every test gets fresh doubles; no test ever reaches a real fhir2 or ledger."""
    fhir, ledger = FakeFhir2(), FakeLedger()
    handler._FHIR, handler._LEDGER = fhir, ledger
    yield fhir, ledger
    handler._FHIR = handler._LEDGER = None


def _input_validator(skill_id: str) -> Draft202012Validator:
    """The skill's $defs/input schema. Nothing validates skill INPUTS in the pipeline yet -- these
    tests are what keep the input schemas honest until it does."""
    schema = json.loads(paths.skill_schema(skill_id).read_text())
    return Draft202012Validator(schema["$defs"]["input"])


def test_dispatch_input_schema_admits_the_payloads_the_orchestrator_sends():
    """The input schema is additionalProperties:false, so every key the orchestrator actually
    sends must be declared -- including the #29 `escalation` slice."""
    v = _input_validator("comms.dispatch")
    v.validate({"studyContext": SAMPLE_CONTEXT})
    v.validate({"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG})
    v.validate({
        "studyContext": SAMPLE_CONTEXT,
        "report": {"diagnosticReportId": "DiagnosticReport/1"},
        "impression": {"criticalFlags": []},
        "verification": {"verificationStatus": "PASS"},
    })


# --- routine: no ack clock ------------------------------------------------------------

async def test_routine_result_posts_to_the_inbox_and_opens_no_ack_clock(stores):
    """A normal study is not a critical result. Opening an ack clock on it is how alert fatigue
    starts -- and it would put a physician on a 60-minute timer for a clean chest X-ray."""
    _, ledger = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("comms.dispatch", out)

    assert out["dispatchStatus"] == "SENT"
    assert out["acrCategory"] == "None"
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox"]
    assert "taskId" not in out                      # no clock
    assert ledger.communications == {} and ledger.tasks == {}   # nothing written


# --- critical: the closed loop --------------------------------------------------------

async def test_critical_finding_records_a_communication_and_opens_the_ack_clock(stores):
    fhir, ledger = stores
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)

    assert out["acrCategory"] == "Cat1"
    assert [c["channel"] for c in out["channelResults"]] == ["ehr-inbox", "oncall-pager"]
    assert out["recipient"] == "Practitioner/dr-order"       # the ordering physician
    assert fhir.orders_read == ["ServiceRequest/sr-1"]

    # The Communication says what we told, and about which order.
    comm = ledger.communications[out["communicationId"]]
    assert comm.subject.reference == "Patient/1"
    assert comm.basedOn[0].reference == "ServiceRequest/sr-1"
    assert comm.recipient[0].reference == "Practitioner/dr-order"
    assert comm.finding_summary == "aortic dissection"

    # The Task is the open loop: it points AT that Communication and is owned by the recipient.
    task = ledger.tasks[out["taskId"]]
    assert task.focus.reference == f"Communication/{comm.id}"
    assert task.owner.reference == "Practitioner/dr-order"
    assert task.status is TaskStatus.REQUESTED
    # Cat1 = contact within 60 minutes.
    assert task.restriction.period.end - task.restriction.period.start == timedelta(minutes=60)
    assert out["deadline"] == task.restriction.period.end.isoformat()


async def test_failed_verification_is_cat2_with_the_slower_clock(stores):
    _, ledger = stores
    out = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "verification": {"verificationStatus": "FAIL", "requiresHumanReview": True},
    })
    validate_skill_output("comms.dispatch", out)
    assert out["acrCategory"] == "Cat2"
    task = ledger.tasks[out["taskId"]]
    assert task.restriction.period.end - task.restriction.period.start == timedelta(minutes=1440)


async def test_no_requester_falls_back_to_on_call_rather_than_dropping_it(stores):
    """A study ingested with an unresolved order (#11) has no requester. A critical finding with
    nobody to tell must not be silently dropped."""
    fhir, ledger = stores
    fhir.requester = None
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SENT"
    assert out["recipient"] == "Practitioner/dr-oncall"


async def test_nobody_to_tell_reports_skipped_not_sent(stores):
    """No requester AND no on-call. SENT would claim a page that never happened -- and a claimed
    page is worse than an admitted failure, because nobody goes looking for it."""
    fhir, ledger = stores
    fhir.requester = None
    ledger.on_call = None
    out = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    validate_skill_output("comms.dispatch", out)
    assert out["dispatchStatus"] == "SKIPPED"
    assert out["channelResults"] == []
    assert ledger.communications == {}       # nothing recorded, because nothing was sent


# --- the OTHER gate: #29's sign-off rung ----------------------------------------------

async def test_signoff_rung_dispatches_its_channels_and_opens_NO_ack_clock(stores):
    """#29's ladder is the "radiologist didn't SIGN" gate. There is no signed report to
    acknowledge, so a Communication/Task here would put the same human on two clocks at once."""
    _, ledger = stores
    payload = {"studyContext": SAMPLE_CONTEXT, "escalation": ESCALATION_RUNG}
    _input_validator("comms.dispatch").validate(payload)
    out = await handle("comms.dispatch", payload)
    validate_skill_output("comms.dispatch", out)

    assert [c["channel"] for c in out["channelResults"]] == ["pager", "sms"]
    assert "taskId" not in out
    assert ledger.communications == {} and ledger.tasks == {}   # no ack loop opened


# --- comms.checkAck -------------------------------------------------------------------

async def test_check_ack_reports_pending_before_the_deadline(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "REQUESTED"
    assert out["overdue"] is False


async def test_check_ack_reports_overdue_once_the_deadline_passes(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    task = ledger.tasks[sent["taskId"]]
    task.restriction.period.end = datetime.now(tz=timezone.utc) - timedelta(minutes=1)

    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "OVERDUE"
    assert out["overdue"] is True


async def test_an_acknowledged_task_is_never_overdue(stores):
    """A physician who acknowledged is done, deadline or not. Reporting OVERDUE here would
    escalate a result that already landed."""
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    task = ledger.tasks[sent["taskId"]]
    task.status = TaskStatus.COMPLETED
    task.restriction.period.end = datetime.now(tz=timezone.utc) - timedelta(minutes=1)

    out = await handle("comms.checkAck",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.checkAck", out)
    assert out["ackStatus"] == "COMPLETED"
    assert out["overdue"] is False


# --- comms.escalate -------------------------------------------------------------------

async def test_escalate_fails_the_open_loop_and_opens_a_new_one_on_call(stores):
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)

    assert out["escalated"] is True
    # The original loop is closed as FAILED -- the audit fact a chart review needs: this
    # notification was never acknowledged by the person it was sent to.
    assert ledger.tasks[sent["taskId"]].status is TaskStatus.FAILED
    # A new loop is open on the on-call provider, carrying the SAME urgency and finding.
    new_comm = ledger.communications[out["newCommunicationId"]]
    assert new_comm.recipient[0].reference == "Practitioner/dr-oncall"
    assert new_comm.category[0].coding[0].code == "Cat1"
    assert "aortic dissection" in new_comm.finding_summary
    assert new_comm.finding_summary.startswith("[ESCALATED]")
    new_task = ledger.tasks[out["newTaskId"]]
    assert new_task.owner.reference == "Practitioner/dr-oncall"
    assert new_task.status is TaskStatus.REQUESTED


async def test_an_escalated_cat2_gets_the_SHORT_window_not_another_24_hours(stores):
    """The escalation window is deliberately NOT the category's window (#52, @sunbiz).

    A Cat2 result must be communicated within 24h. If nobody acknowledges and we escalate, handing
    on-call a fresh 24h clock would mean the finding could take 48 hours to land -- the escalation
    would make the deadline WORSE. An escalated result is already late, so it runs on one short
    window regardless of category.

    This is also the fix for a dead read: _escalate used to take its window from
    payload.get("ackMinutes"), but comms.escalate's input schema admits only studyContext + taskId
    with additionalProperties false, so that read could never receive data and every escalated loop
    silently took a 60-minute fallback nobody had chosen.
    """
    _, ledger = stores
    # FAIL + requiresHumanReview -> Cat2, whose own ack window is 24 hours.
    sent = await handle("comms.dispatch", {
        "studyContext": SAMPLE_CONTEXT,
        "verification": {"verificationStatus": "FAIL", "requiresHumanReview": True, "issues": []},
    })
    assert sent["acrCategory"] == "Cat2"
    original = ledger.tasks[sent["taskId"]].restriction.period.end

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    escalated = ledger.tasks[out["newTaskId"]].restriction.period.end

    # The original clock really was the Cat2 window (~24h), and the escalated one is ~1h.
    assert original - datetime.now(timezone.utc) > timedelta(hours=20)
    assert escalated - datetime.now(timezone.utc) < timedelta(hours=2)
    assert escalated < original, "escalation must not push the deadline further out"


async def test_the_escalation_window_is_configurable(stores, monkeypatch):
    """Same knob shape as the per-category windows (CRITCOM_CAT*_ACK_TIMEOUT_MINUTES): a deployment
    that chases harder or softer sets it, rather than editing a hardcoded 60."""
    monkeypatch.setenv("CRITCOM_ESCALATION_ACK_TIMEOUT_MINUTES", "15")
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    escalated = ledger.tasks[out["newTaskId"]].restriction.period.end

    assert escalated - datetime.now(timezone.utc) < timedelta(minutes=16)


async def test_escalate_with_nobody_on_call_says_so_instead_of_claiming_success(stores):
    """An unescalatable critical result is exactly the thing a human must hear about. Returning
    escalated=true with no recipient would bury it."""
    _, ledger = stores
    sent = await handle("comms.dispatch", {"studyContext": SAMPLE_CONTEXT, "impression": CRITICAL})
    ledger.on_call = None

    out = await handle("comms.escalate",
                       {"studyContext": SAMPLE_CONTEXT, "taskId": sent["taskId"]})
    validate_skill_output("comms.escalate", out)
    assert out["escalated"] is False
    assert "on-call" in out["reason"]
    assert ledger.tasks[sent["taskId"]].status is TaskStatus.FAILED   # still not acknowledged


async def test_unexpected_skill_raises():
    with pytest.raises(ValueError):
        await handle("comms.sendCarrierPigeon", {"studyContext": SAMPLE_CONTEXT})
