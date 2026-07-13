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


# --- report-body parsing feeds the PI rules (#22). An inline `conclusion` stands in for the fhir2
#     fetch so these stay hermetic (the handler prefers it over a network read). -----------------

def _rule_ids(out: dict) -> set[str]:
    return {i["ruleId"] for i in out["issues"]}


async def test_mammo_rules_fire_from_parsed_body():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "IMPRESSION: BI-RADS 4 suspicious mass, left breast."},
        "impression": {"impressionText": "Suspicious mass.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    ids = _rule_ids(out)
    assert "mammo-actionable-needs-followup" in ids  # BI-RADS 4 with no recommendation
    assert "mammo-density-stated" in ids             # mammo read with no density stated
    assert out["verificationStatus"] == "WARN"


async def test_out_of_range_birads_flagged():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "BI-RADS 7. Breast density C."},
        "impression": {"impressionText": "See report.", "criticalFlags": [], "recommendations": [{"text": "f/u"}]},
    })
    validate_skill_output("report.verify", out)
    assert "mammo-birads-code-valid" in _rule_ids(out)


async def test_laterality_mismatch_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "Nodule seen in the right upper lobe."},
        "impression": {"impressionText": "Left upper lobe nodule; follow-up advised.",
                       "criticalFlags": [], "recommendations": [{"text": "CT in 3 months"}]},
    })
    validate_skill_output("report.verify", out)
    assert "laterality-consistency" in _rule_ids(out)


async def test_critical_in_body_but_unflagged_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "Moderate pneumothorax on the right."},
        "impression": {"impressionText": "Findings noted.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "critical-finding-unflagged" in _rule_ids(out)


async def test_findings_without_impression_warns():
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "FINDINGS: Stable chest. No acute process.\nTECHNIQUE: PA and lateral."},
        "impression": {"impressionText": "Stable.", "criticalFlags": [], "recommendations": [{"text": "x"}]},
    })
    validate_skill_output("report.verify", out)
    assert "impression-section-present" in _rule_ids(out)


async def test_clean_report_with_narrative_still_passes():
    # A well-formed report body must not trip any rule (guards against false positives).
    out = await handle("report.verify", {
        "studyContext": SAMPLE_CONTEXT,
        "report": {"conclusion": "IMPRESSION: No acute cardiopulmonary process."},
        "impression": {"impressionText": "No acute findings.", "criticalFlags": [], "recommendations": []},
    })
    validate_skill_output("report.verify", out)
    assert out["verificationStatus"] == "PASS"
    assert out["issues"] == []
