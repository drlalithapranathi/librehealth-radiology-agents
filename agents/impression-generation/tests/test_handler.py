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


# --- negation-aware scan (#78) ----------------------------------------------------------------

async def test_pertinent_negative_report_sets_no_critical_flags():
    """#78 acceptance: a normal report written as pertinent negatives must NOT flag. Before the
    negation window, "No pneumothorax, ..." matched \\bpneumothorax\\b, set criticalFlags, FAILed
    verification, and parked every normal study at the sign-off gate."""
    report = {"conclusion": "No pneumothorax, pleural effusion, or focal consolidation."}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    validate_skill_output("impression.generate", out)
    assert out["criticalFlags"] == []
    assert out["impressionText"].startswith("No acute findings")


async def test_stated_positive_finding_still_flags():
    report = {"conclusion": "Large right-sided pneumothorax."}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    assert out["criticalFlags"] == [{"label": "pneumothorax", "severity": "critical"}]


async def test_indication_section_naming_the_suspicion_does_not_flag():
    """#78 regression (reproduced): a production conclusion carries the full sectioned narrative, and the
    INDICATION names the SUSPICION ("evaluate for pneumothorax") -- which is not a finding. Scanning
    it re-flagged every normal study ordered to exclude the very thing it excluded."""
    report = {"conclusion": ("INDICATION: Chest pain, evaluate for pneumothorax.\n"
                             "FINDINGS: Lungs are clear.\n"
                             "IMPRESSION: No pneumothorax.")}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    assert out["criticalFlags"] == []


async def test_a_positive_finding_in_the_findings_section_still_flags():
    report = {"conclusion": ("INDICATION: Chest pain.\n"
                             "FINDINGS: Large right-sided pneumothorax.\n"
                             "IMPRESSION: Pneumothorax requiring intervention.")}
    out = await handle("impression.generate", {"studyContext": SAMPLE_CONTEXT, "report": report})
    assert out["criticalFlags"] == [{"label": "pneumothorax", "severity": "critical"}]


async def test_negation_does_not_bleed_between_two_ai_finding_labels():
    """#78 regression (reproduced): labels used to be space-joined into one clause, so a 'No hemorrhage' label
    from one tool silenced a positive 'Mass ...' label from the next. Each label is its own scan."""
    out = await handle("impression.generate", {
        "studyContext": SAMPLE_CONTEXT,
        "aiFindings": {"findings": [
            {"toolId": "ich-detect", "status": "COMPLETE", "label": "No hemorrhage"},
            {"toolId": "lung-nodule-detect", "status": "COMPLETE",
             "label": "Mass in the left upper lobe (p=0.88)"},
        ]},
    })
    assert out["criticalFlags"] == [{"label": "mass lesion", "severity": "critical"}]


async def test_report_negation_does_not_suppress_a_positive_ai_finding():
    """The report conclusion and the aiFindings labels are scanned SEPARATELY. A "no pneumothorax"
    in the report must not bleed its negation across a POSITIVE pneumothorax the model reported
    (#71 emits exactly that label) -- concatenating the two texts would have silenced a real find."""
    out = await handle("impression.generate", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "No pneumothorax."},
        "aiFindings": {"findings": [
            {"toolId": "pneumothorax-detect", "status": "COMPLETE",
             "label": "Pneumothorax (screening p=0.91); screening signal only, not a read"},
        ]},
    })
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
