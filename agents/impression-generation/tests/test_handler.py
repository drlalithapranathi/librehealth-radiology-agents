"""Contract test: handler output must validate against its /contracts schema."""
import pytest

import handler
from handler import handle
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_test",
    "study": {
        "studyInstanceUID": "1.2.3",
        "orthancStudyId": "abc123",
        "modality": "CT",
        "studyDescription": "CT CHEST W/O",
    },
    "patient": {"fhirPatientId": "Patient/1"},
    "order": {"priority": "routine", "reasonCode": ["R91.8"]},
    "meta": {"traceId": "trc_x", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_output_conforms_to_contract():
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("impression.generate", out)  # raises ContractError on violation
    assert out["workflowId"] == "wf_test"


async def test_critical_keyword_sets_flags_and_conforms_to_contract():
    report = {"conclusion": "Findings show a large pneumothorax requiring urgent attention."}
    out = await handle(
        "impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report}
    )
    validate_skill_output("impression.generate", out)  # raises ContractError on violation
    assert out["criticalFlags"] == [{"label": "pneumothorax", "severity": "critical"}]


class _FakeFhir:
    """Stand-in for Fhir2Client.get_report_conclusion (no live server)."""
    def __init__(self, conclusion=None, error=None):
        self._conclusion = conclusion
        self._error = error
        self.calls: list[str] = []

    async def get_report_conclusion(self, report_id: str):
        self.calls.append(report_id)
        if self._error:
            raise self._error
        return self._conclusion


@pytest.fixture(autouse=True)
def _reset_fhir():
    handler._FHIR = None
    yield
    handler._FHIR = None


async def test_fetches_conclusion_from_fhir2_by_report_id():
    # The lean finalized event carries no narrative (Golden rule 2); the handler must fetch the
    # report content from fhir2 by its id to detect a critical (#16).
    fake = _FakeFhir(conclusion="CT shows an acute aortic dissection.")
    handler._FHIR = fake
    report = {"diagnosticReportId": "DiagnosticReport/demo-1", "status": "final"}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    validate_skill_output("impression.generate", out)
    assert fake.calls == ["DiagnosticReport/demo-1"]  # fetched from source, not read off the message
    assert out["criticalFlags"] == [{"label": "aortic dissection", "severity": "critical"}]


async def test_fhir2_failure_degrades_to_a_valid_no_flags_draft():
    handler._FHIR = _FakeFhir(error=RuntimeError("fhir2 unreachable"))
    report = {"diagnosticReportId": "DiagnosticReport/demo-1"}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    validate_skill_output("impression.generate", out)  # best-effort: still a valid draft
    assert out["criticalFlags"] == []


async def test_word_boundary_avoids_substring_false_positive():
    # "massive" must NOT trip the "mass" -> mass lesion keyword.
    report = {"conclusion": "Massive pleural effusion identified."}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    assert out["criticalFlags"] == []


async def test_presign_ai_findings_set_flags_when_no_report_yet():
    # Pre-sign (#26): no report exists yet, so aiFindings is the only signal available.
    ai_findings = {
        "toolsSelected": [{"toolId": "t1", "status": "COMPLETE"}],
        "findings": [{"toolId": "t1", "label": "pulmonary embolism", "status": "COMPLETE"}],
    }
    out = await handle(
        "impression.generate", {"studyContext": SAMPLE_CONTEXT, "aiFindings": ai_findings}
    )
    validate_skill_output("impression.generate", out)
    assert out["criticalFlags"] == [{"label": "pulmonary embolism", "severity": "critical"}]


async def test_presign_ignores_non_complete_findings():
    # A STUBBED/ERROR finding carries no real label in v1 and must not fabricate a flag.
    ai_findings = {
        "findings": [
            {"toolId": "t1", "label": "mass lesion", "status": "STUBBED"},
            {"toolId": "t2", "label": "fracture", "status": "ERROR"},
        ],
    }
    out = await handle(
        "impression.generate", {"studyContext": SAMPLE_CONTEXT, "aiFindings": ai_findings}
    )
    assert out["criticalFlags"] == []


async def test_postsign_merges_report_and_ai_findings_signals():
    # Both signals passed forward post-sign: an aiFindings-only hit still surfaces.
    report = {"conclusion": "No acute intracranial process."}
    ai_findings = {"findings": [{"toolId": "t1", "label": "fracture", "status": "COMPLETE"}]}
    out = await handle(
        "impression.generate",
        {"studyContext": SAMPLE_CONTEXT, "report": report, "aiFindings": ai_findings},
    )
    assert out["criticalFlags"] == [{"label": "fracture", "severity": "critical"}]
