"""Unit tests for the RIS poller mapping + cursor logic (issue #12).

Covers acceptance box 1 (finalized report signals the CORRECT waiting workflow, via the right
signal) and box 2 (cursor advances to the high-water mark, dedups at the boundary, no double-
processing). Skipped when the orchestrator's deps (temporalio/fastapi) aren't installed.
"""
from __future__ import annotations

import asyncio

import pytest

ingress = pytest.importorskip("orchestrator.ingress", reason="orchestrator deps not installed")


def _ctx(wf, accession=None, service_request=None):
    return {
        "workflowId": wf,
        "study": {"accessionNumber": accession} if accession else {},
        "order": {"fhirServiceRequestId": service_request} if service_request else {},
    }


def _report(rid, cursor, accession=None, service_request=None):
    return {"diagnosticReportId": rid, "lastUpdatedCursor": cursor,
            "accessionNumber": accession, "serviceRequestRef": service_request}


@pytest.fixture(autouse=True)
def _fresh_store():
    """Each test gets an isolated in-memory durable store (#6 replaced the in-process dict)."""
    ingress._STORE = ingress.IngressStore(":memory:")
    yield
    ingress._STORE.close()
    ingress._STORE = None


# ---- mapping (acceptance box 1) --------------------------------------------------

def test_index_and_lookup_by_accession_and_service_request():
    ingress._index_workflow(_ctx("wf_a", accession="ACC-1"))
    ingress._index_workflow(_ctx("wf_b", service_request="ServiceRequest/sr-2"))

    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-1"}) == "wf_a"
    assert ingress._workflow_id_for_report({"serviceRequestRef": "ServiceRequest/sr-2"}) == "wf_b"
    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-unknown"}) is None


def test_service_request_preferred_over_accession():
    ingress._index_workflow(_ctx("wf_sr", service_request="ServiceRequest/sr-9"))
    ingress._index_workflow(_ctx("wf_acc", accession="ACC-9"))
    report = {"serviceRequestRef": "ServiceRequest/sr-9", "accessionNumber": "ACC-9"}
    assert ingress._workflow_id_for_report(report) == "wf_sr"


# ---- signalling (acceptance box 1) -----------------------------------------------

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


def test_process_batch_signals_correct_workflow_with_report_finalized_signal():
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    reports = [
        _report("DiagnosticReport/r1", "t1", accession="ACC-1"),        # mapped
        _report("DiagnosticReport/r2", "t2", accession="ACC-unknown"),  # unmapped -> skipped
    ]
    client = _FakeClient()
    newly, failed = asyncio.run(ingress._process_batch(client, reports, set()))

    # Signalled the mapped report to the right workflow, via the report_finalized signal.
    assert client.signals == [("wf_1", ingress.StudyWorkflow.report_finalized, reports[0])]
    assert newly == {"DiagnosticReport/r1"}
    assert failed == []  # the unmapped report is dropped, not queued for retry


def test_process_batch_routes_two_reports_to_two_workflows():
    ingress._index_workflow(_ctx("wf_a", accession="ACC-A"))
    ingress._index_workflow(_ctx("wf_b", service_request="ServiceRequest/sr-B"))
    reports = [
        _report("DiagnosticReport/ra", "t1", accession="ACC-A"),
        _report("DiagnosticReport/rb", "t2", service_request="ServiceRequest/sr-B"),
    ]
    client = _FakeClient()
    asyncio.run(ingress._process_batch(client, reports, set()))
    assert {wf for wf, _sig, _arg in client.signals} == {"wf_a", "wf_b"}


# ---- failed-signal retry: hold the cursor, never lose a sign-off (#29) ------------

def test_advance_cursor_holds_at_failed_report_not_high_water():
    r1 = _report("DiagnosticReport/r1", "2026-06-27T12:00:00Z")  # mapped, signal FAILED
    r2 = _report("DiagnosticReport/r2", "2026-06-27T12:05:00Z")  # signalled OK (the high water)
    cursor, kept = ingress._advance_cursor(
        "2026-06-27T11:00:00Z", "2026-06-27T12:05:00Z", [r1, r2], {"DiagnosticReport/r2"}, failed=[r1])
    # Held at the failure so the ge-window re-returns r1 next poll; previously the cursor jumped
    # to 12:05 and r1's sign-off was lost.
    assert cursor == "2026-06-27T12:00:00Z"
    # The later success stays in the dedup set so the wider re-scan cannot double-signal it.
    assert kept == {"DiagnosticReport/r2"}


def test_advance_cursor_failure_at_the_boundary_keeps_accumulating():
    r = _report("DiagnosticReport/rB", "t0")  # failed AT the current cursor second
    assert ingress._advance_cursor("t0", "t9", [r], {"other"}, failed=[r]) == ("t0", {"other"})


def test_advance_cursor_failure_without_timestamp_pins_cursor():
    r = _report("DiagnosticReport/rX", None)  # no meta.lastUpdated: cannot place it in time
    assert ingress._advance_cursor("t0", "t9", [r], {"other"}, failed=[r]) == ("t0", {"other"})


def test_failed_report_is_retried_next_poll_and_nothing_double_signals():
    """Two-poll walk-through of the #29 scenario: r1's signal fails while the later r2 succeeds.
    Poll 2's ge-window (held at r1) re-returns both; r1 is retried, r2 is deduped."""
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    ingress._index_workflow(_ctx("wf_2", accession="ACC-2"))
    r1 = _report("DiagnosticReport/r1", "2026-06-27T12:00:00Z", accession="ACC-1")
    r2 = _report("DiagnosticReport/r2", "2026-06-27T12:05:00Z", accession="ACC-2")
    high_water = "2026-06-27T12:05:00Z"

    class _FlakyClient(_FakeClient):
        """wf_1 is unreachable on the first attempt only (a transient Temporal blip)."""
        def __init__(self):
            super().__init__()
            self.tripped = False

        def get_workflow_handle(self, wf_id):
            if wf_id == "wf_1" and not self.tripped:
                self.tripped = True
                return _RaisingHandle()
            return super().get_workflow_handle(wf_id)

    client = _FlakyClient()

    # Poll 1: r1 fails, r2 succeeds -> cursor holds at r1, r2 kept for dedup.
    newly, failed = asyncio.run(ingress._process_batch(client, [r1, r2], set()))
    assert (newly, failed) == ({"DiagnosticReport/r2"}, [r1])
    cursor, dedup = ingress._advance_cursor("2026-06-27T11:00:00Z", high_water, [r1, r2], newly, failed)
    assert cursor == "2026-06-27T12:00:00Z"

    # Poll 2: the ge-window re-returns both; only r1 is (re)signalled.
    newly2, failed2 = asyncio.run(ingress._process_batch(client, [r1, r2], dedup))
    assert (newly2, failed2) == ({"DiagnosticReport/r1"}, [])
    cursor2, dedup2 = ingress._advance_cursor(cursor, high_water, [r1, r2], dedup | newly2, failed2)
    assert cursor2 == high_water

    # Each workflow got its sign-off exactly once, in the end.
    assert client.signals == [
        ("wf_2", ingress.StudyWorkflow.report_finalized, r2),
        ("wf_1", ingress.StudyWorkflow.report_finalized, r1),
    ]


# ---- reconcile must not evict on outage: unreachable != closed (#29) --------------

class _DescribeClient:
    """Fake client whose describe() outcome is scripted per workflow id."""
    def __init__(self, outcomes):
        self._outcomes = outcomes  # wf_id -> exception to raise

    def get_workflow_handle(self, wf_id):
        outcome = self._outcomes[wf_id]

        class _H:
            async def describe(self):
                raise outcome

        return _H()


def test_reconcile_keeps_rows_when_temporal_unreachable():
    """A Temporal outage is exactly when signals fail and the cursor holds (#29) — a reconcile
    sweep during it must NOT evict the index, or the held retry becomes a permanent loss."""
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    client = _DescribeClient({"wf_1": RuntimeError("temporal unreachable")})
    pruned = asyncio.run(ingress._reconcile_index(client))
    assert pruned == 0
    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-1"}) == "wf_1"


def test_reconcile_evicts_on_affirmative_not_found():
    """A NOT_FOUND is an affirmative 'gone' -> the row is reclaimed (retention GC still works)."""
    from temporalio.service import RPCError, RPCStatusCode

    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    client = _DescribeClient({"wf_1": RPCError("no such workflow", RPCStatusCode.NOT_FOUND, b"")})
    pruned = asyncio.run(ingress._reconcile_index(client))
    assert pruned == 1
    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-1"}) is None


def test_process_batch_dedups_already_signalled():
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    reports = [_report("DiagnosticReport/r1", "t1", accession="ACC-1")]
    client = _FakeClient()
    newly, _failed = asyncio.run(ingress._process_batch(client, reports, {"DiagnosticReport/r1"}))
    assert client.signals == []  # already signalled at this boundary -> not re-sent
    assert newly == set()


class _RaisingHandle:
    async def signal(self, *_a):
        raise RuntimeError("temporal unreachable")


class _RaisingClient:
    def get_workflow_handle(self, _wf):
        return _RaisingHandle()


def test_process_batch_signal_failure_is_swallowed_and_reported():
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    reports = [_report("DiagnosticReport/r1", "t1", accession="ACC-1")]
    newly, failed = asyncio.run(ingress._process_batch(_RaisingClient(), reports, set()))
    assert newly == set()      # failed signal is not counted as signalled...
    assert failed == reports   # ...and is handed back so the cursor holds for a retry (#29)


# ---- cursor / dedup (acceptance box 2) -------------------------------------------

def test_advance_cursor_moves_to_high_water_and_keeps_boundary_ids():
    reports = [
        _report("DiagnosticReport/r1", "2026-06-27T12:00:00Z"),
        _report("DiagnosticReport/r2", "2026-06-27T12:05:00Z"),  # the high-water boundary
    ]
    signalled = {"DiagnosticReport/r1", "DiagnosticReport/r2"}
    cursor, kept = ingress._advance_cursor("t0", "2026-06-27T12:05:00Z", reports, signalled)
    assert cursor == "2026-06-27T12:05:00Z"
    # Only the boundary id is retained for dedup; the older one falls out of the ge window.
    assert kept == {"DiagnosticReport/r2"}


def test_advance_cursor_noop_when_high_water_unchanged_or_missing():
    signalled = {"x"}
    assert ingress._advance_cursor("t2", "t2", [], signalled) == ("t2", {"x"})
    assert ingress._advance_cursor("t2", None, [], signalled) == ("t2", {"x"})
