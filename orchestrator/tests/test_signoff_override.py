"""#57: the authenticated endpoint that releases the sign-off gate — the signal's missing producer.

`signoff_acknowledged` is what AWAITING_SIGNOFF waits on, and before #57 nothing in production sent
it: it existed in workflow.py and in test files, and nowhere else. So a report that FAILed
verification with requiresHumanReview paged its way up the escalation ladder and then waited
forever. It never reached COMMUNICATE — so the critical finding that made verification FAIL was
never dispatched — and it never archived (#56).

These tests cover the endpoint. The gate's own behaviour (released -> COMMUNICATE with the FAIL and
the acknowledgement both on the record, and no re-verify) is in test_workflow_gates.py; the replay
safety of the change is in test_signoff_override_replay.py.

Skipped when the orchestrator's deps aren't installed.
"""
from __future__ import annotations

import asyncio

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from fastapi import HTTPException  # noqa: E402

from orchestrator.workflow import StudyWorkflow  # noqa: E402

WHO = "Practitioner/dr-rao"
WHY = "reviewed with the referrer; the flagged critical was already communicated by phone"


class _FakeHandle:
    def __init__(self, sink, wf_id, boom=False):
        self._sink, self._wf, self._boom = sink, wf_id, boom

    async def signal(self, signal, arg):
        if self._boom:
            raise RuntimeError("temporal unavailable")
        self._sink.append((self._wf, signal, arg))


class _FakeClient:
    def __init__(self, boom=False):
        self.signals: list = []
        self._boom = boom

    def get_workflow_handle(self, wf_id):
        return _FakeHandle(self.signals, wf_id, self._boom)


@pytest.fixture
def temporal(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(ingress, "_client", client)
    monkeypatch.setattr(ingress, "SIGNOFF_OVERRIDE_TOKEN", "s3cret")
    return client


def _call(body: dict, token: str = "s3cret", wf: str = "wf_1"):
    return asyncio.run(ingress.signoff_override(wf, body, x_signoff_token=token))


# --- the happy path: a named human, a stated reason, one signal ----------------------


def test_an_override_signals_the_workflow_with_who_and_why(temporal):
    """The signal payload IS the audit record of a human waiving a safety verdict on a report."""
    out = _call({"acknowledgedBy": WHO, "reason": WHY})

    assert out["acknowledged"] is True
    assert out["acknowledgedBy"] == WHO
    assert len(temporal.signals) == 1
    wf, signal, ack = temporal.signals[0]
    assert wf == "wf_1"
    assert signal is StudyWorkflow.signoff_acknowledged
    assert ack["acknowledgedBy"] == WHO
    assert ack["reason"] == WHY
    assert ack["acknowledgedAt"]                       # stamped by ingress, not by the caller


# --- authentication: this endpoint waives a safety verdict ----------------------------


def test_an_unconfigured_token_refuses_to_act_rather_than_accepting_anyone(monkeypatch):
    """The Orthanc webhook and the A2A callback accept unauthenticated calls when their token is
    unset — a deliberate compose posture, because they only DELIVER facts. This endpoint releases a
    gate that verification held on clinical-safety grounds, so the same posture would be a hole: an
    override nobody can be identified with is not an override.

    So with no token configured it 503s (refuses to act) rather than defaulting to anonymous.
    """
    client = _FakeClient()
    monkeypatch.setattr(ingress, "_client", client)
    monkeypatch.setattr(ingress, "SIGNOFF_OVERRIDE_TOKEN", "")

    with pytest.raises(HTTPException) as e:
        _call({"acknowledgedBy": WHO, "reason": WHY}, token="")
    assert e.value.status_code == 503
    assert client.signals == []                        # the gate is still held


def test_a_wrong_token_is_rejected(temporal):
    with pytest.raises(HTTPException) as e:
        _call({"acknowledgedBy": WHO, "reason": WHY}, token="guess")
    assert e.value.status_code == 401
    assert temporal.signals == []


# --- the audit record must be real ----------------------------------------------------


@pytest.mark.parametrize("body", [
    {},
    {"acknowledgedBy": WHO},                            # no reason
    {"reason": WHY},                                    # nobody
    {"acknowledgedBy": "   ", "reason": WHY},           # blank author
    {"acknowledgedBy": WHO, "reason": "  "},            # blank reason
])
def test_an_override_without_who_and_why_is_rejected(temporal, body):
    """A blank author or a blank reason would record an un-auditable waiver of a safety verdict.
    Reject it rather than write an empty row into the workflow history."""
    with pytest.raises(HTTPException) as e:
        _call(body)
    assert e.value.status_code == 422
    assert temporal.signals == []


def test_an_oversized_reason_is_rejected(temporal):
    """The reason is a clinician's audit note, not a report. It rides in workflow history, which is
    replayed on every worker pickup, so an unbounded paste is a durable cost on every replay."""
    with pytest.raises(HTTPException) as e:
        _call({"acknowledgedBy": WHO, "reason": "x" * (ingress.SIGNOFF_REASON_MAX + 1)})
    assert e.value.status_code == 422
    assert temporal.signals == []


# --- failure to signal must not look like success -------------------------------------


def test_a_failed_signal_reports_the_gate_is_still_held(monkeypatch):
    """Temporal down, or an unknown workflowId. Returning 202 here would tell a radiologist the
    study was released when it was not -- they would walk away from a gate that is still shut."""
    client = _FakeClient(boom=True)
    monkeypatch.setattr(ingress, "_client", client)
    monkeypatch.setattr(ingress, "SIGNOFF_OVERRIDE_TOKEN", "s3cret")

    with pytest.raises(HTTPException) as e:
        _call({"acknowledgedBy": WHO, "reason": WHY})
    assert e.value.status_code == 502
    assert "still held" in e.value.detail
