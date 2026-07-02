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


# ---- mapping (acceptance box 1) --------------------------------------------------

def test_index_and_lookup_by_accession_and_service_request():
    ingress._WORKFLOW_INDEX.clear()
    ingress._index_workflow(_ctx("wf_a", accession="ACC-1"))
    ingress._index_workflow(_ctx("wf_b", service_request="ServiceRequest/sr-2"))

    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-1"}) == "wf_a"
    assert ingress._workflow_id_for_report({"serviceRequestRef": "ServiceRequest/sr-2"}) == "wf_b"
    assert ingress._workflow_id_for_report({"accessionNumber": "ACC-unknown"}) is None


def test_service_request_preferred_over_accession():
    ingress._WORKFLOW_INDEX.clear()
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
    ingress._WORKFLOW_INDEX.clear()
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    reports = [
        _report("DiagnosticReport/r1", "t1", accession="ACC-1"),        # mapped
        _report("DiagnosticReport/r2", "t2", accession="ACC-unknown"),  # unmapped -> skipped
    ]
    client = _FakeClient()
    newly = asyncio.run(ingress._process_batch(client, reports, set()))

    # Signalled the mapped report to the right workflow, via the report_finalized signal.
    assert client.signals == [("wf_1", ingress.StudyWorkflow.report_finalized, reports[0])]
    assert newly == {"DiagnosticReport/r1"}


def test_process_batch_routes_two_reports_to_two_workflows():
    ingress._WORKFLOW_INDEX.clear()
    ingress._index_workflow(_ctx("wf_a", accession="ACC-A"))
    ingress._index_workflow(_ctx("wf_b", service_request="ServiceRequest/sr-B"))
    reports = [
        _report("DiagnosticReport/ra", "t1", accession="ACC-A"),
        _report("DiagnosticReport/rb", "t2", service_request="ServiceRequest/sr-B"),
    ]
    client = _FakeClient()
    asyncio.run(ingress._process_batch(client, reports, set()))
    assert {wf for wf, _sig, _arg in client.signals} == {"wf_a", "wf_b"}


def test_process_batch_dedups_already_signalled():
    ingress._WORKFLOW_INDEX.clear()
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))
    reports = [_report("DiagnosticReport/r1", "t1", accession="ACC-1")]
    client = _FakeClient()
    newly = asyncio.run(ingress._process_batch(client, reports, {"DiagnosticReport/r1"}))
    assert client.signals == []  # already signalled at this boundary -> not re-sent
    assert newly == set()


def test_process_batch_signal_failure_is_swallowed():
    ingress._WORKFLOW_INDEX.clear()
    ingress._index_workflow(_ctx("wf_1", accession="ACC-1"))

    class _RaisingHandle:
        async def signal(self, *_a):
            raise RuntimeError("temporal unreachable")

    class _RaisingClient:
        def get_workflow_handle(self, _wf):
            return _RaisingHandle()

    reports = [_report("DiagnosticReport/r1", "t1", accession="ACC-1")]
    newly = asyncio.run(ingress._process_batch(_RaisingClient(), reports, set()))
    assert newly == set()  # failed signal is not counted as signalled


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
