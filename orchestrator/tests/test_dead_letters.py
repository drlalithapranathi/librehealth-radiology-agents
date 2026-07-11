"""Dead-letter capture for permanently dropped sign-offs (#29).

The held-cursor retry (#29, merged) keeps re-delivering a failed sign-off until reconciliation
evicts the target workflow's index rows; at that point the report re-enters as UNMAPPED and used
to vanish with a log line. These tests pin the new behavior: failed attempts are tracked durably,
the final unmapped re-entry of a formerly-mapped report becomes a dead-letter row (IDs only, no
PHI), and routine never-ours fhir2 reports stay plain noise. Skipped when the orchestrator's deps
aren't installed.
"""
from __future__ import annotations

import asyncio

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")
from orchestrator.ingress_store import IngressStore  # noqa: E402


def _ctx(wf, accession=None):
    return {"workflowId": wf,
            "study": {"accessionNumber": accession} if accession else {},
            "order": {}}


def _report(rid, cursor, accession=None):
    return {"diagnosticReportId": rid, "lastUpdatedCursor": cursor,
            "accessionNumber": accession, "serviceRequestRef": None}


@pytest.fixture(autouse=True)
def _fresh_store():
    ingress._STORE = IngressStore(":memory:")
    yield
    ingress._STORE.close()
    ingress._STORE = None


class _FakeHandle:
    def __init__(self, sink, wf_id):
        self._sink, self._wf = sink, wf_id

    async def signal(self, signal, arg):
        self._sink.append((self._wf, signal, arg))


class _FakeClient:
    def __init__(self):
        self.signals: list = []

    def get_workflow_handle(self, wf_id):
        return _FakeHandle(self.signals, wf_id)


class _RaisingHandle:
    async def signal(self, *_a):
        raise RuntimeError("temporal unreachable")


class _RaisingClient:
    def get_workflow_handle(self, _wf):
        return _RaisingHandle()


# ---- failure tracking --------------------------------------------------------------

def test_failed_signal_is_tracked_and_attempts_accumulate():
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    report = _report("DiagnosticReport/r1", "t1", accession="ACC-1")

    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))
    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))

    record = ingress._store().failed_signal_for("DiagnosticReport/r1")
    assert record["workflowId"] == "wf_1"
    assert record["attempts"] == 2
    assert ingress._store().dead_letters() == []  # still mapped: retrying, not dead


def test_successful_delivery_retires_the_failure_record():
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    report = _report("DiagnosticReport/r1", "t1", accession="ACC-1")

    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))   # fails once
    asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))     # then lands

    assert ingress._store().failed_signal_for("DiagnosticReport/r1") is None
    assert ingress._store().dead_letters() == []


# ---- the give-up path becomes a dead letter -----------------------------------------

def test_evicted_workflow_turns_failing_report_into_a_dead_letter():
    """The full #29 give-up sequence: signal fails -> retry held -> reconciliation evicts the
    workflow -> the report re-enters unmapped -> dead-lettered with its workflow and attempts."""
    ingress._index_workflow(_ctx("wf_gone", accession="ACC-1"))
    report = _report("DiagnosticReport/r1", "t1", accession="ACC-1")

    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))  # attempt 1 fails
    ingress._store().evict_workflow("wf_gone")                              # reconcile gives up
    newly, failed = asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))

    assert newly == set() and failed == []  # unmapped: not signalled, not retried
    (letter,) = ingress._store().dead_letters()
    assert letter["reportId"] == "DiagnosticReport/r1"
    assert letter["workflowId"] == "wf_gone"
    assert letter["attempts"] == 1
    assert ingress._store().failed_signal_for("DiagnosticReport/r1") is None  # tracking retired


def test_dead_letter_is_idempotent_across_rescans():
    """The boundary re-scan can re-return the same unmapped report; one dead-letter row only."""
    ingress._index_workflow(_ctx("wf_gone", accession="ACC-1"))
    report = _report("DiagnosticReport/r1", "t1", accession="ACC-1")

    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))
    ingress._store().evict_workflow("wf_gone")
    asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))
    asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))  # re-scan

    assert len(ingress._store().dead_letters()) == 1


def test_never_ours_unmapped_report_is_not_dead_lettered():
    """Routine fhir2 noise (a finalized report for a study we never tracked) stays a log line."""
    report = _report("DiagnosticReport/foreign", "t1", accession="ACC-nobody")
    newly, failed = asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))

    assert newly == set() and failed == []
    assert ingress._store().dead_letters() == []


# ---- durability + the admin surface --------------------------------------------------

def test_failure_tracking_and_dead_letters_survive_a_restart(tmp_path):
    """An ingress restart mid-retry must not forget the failure history — otherwise the eventual
    give-up would look 'never ours' and slip through as noise (the exact gap this closes)."""
    db = str(tmp_path / "ingress_state.db")
    ingress._STORE.close()
    ingress._STORE = IngressStore(db)
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    report = _report("DiagnosticReport/r1", "t1", accession="ACC-1")
    asyncio.run(ingress._process_batch(_RaisingClient(), [report], set()))

    ingress._STORE.close()
    ingress._STORE = IngressStore(db)  # restart
    assert ingress._store().failed_signal_for("DiagnosticReport/r1")["attempts"] == 1

    ingress._store().evict_workflow("wf_1")
    asyncio.run(ingress._process_batch(_FakeClient(), [report], set()))
    ingress._STORE.close()
    ingress._STORE = IngressStore(db)  # restart again
    (letter,) = ingress._store().dead_letters()
    assert letter["workflowId"] == "wf_1"


def test_admin_endpoint_lists_dead_letters():
    ingress._store().add_dead_letter("DiagnosticReport/r9", "wf_9", 3,
                                     "workflow evicted while its sign-off signal was still failing",
                                     "2026-07-10T00:00:00Z")
    body = asyncio.run(ingress.dead_letters())
    assert body["count"] == 1
    assert body["deadLetters"][0]["reportId"] == "DiagnosticReport/r9"
    assert body["deadLetters"][0]["workflowId"] == "wf_9"
