"""Verification contract test + a rule-firing assertion."""
from handler import handle
from radagent_common.validation import validate_skill_output

SAMPLE_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_test",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_clean_report_passes():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "PASS"
    assert out["requiresHumanReview"] is False


async def test_critical_flag_fails_and_requires_review():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "impression": {"impressionText": "Tension pneumothorax.",
                       "criticalFlags": [{"label": "tension pneumothorax", "severity": "critical"}],
                       "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "FAIL"
    assert out["requiresHumanReview"] is True
    assert any(i["ruleId"] == "critical-comm-required" for i in out["issues"])
