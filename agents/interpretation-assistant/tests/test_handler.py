"""Contract test + selection tests for interpretation assistant."""
from handler import handle
from registry import select_tools
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
    out = await handle("interpretation.runTools", {"studyContext": SAMPLE_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    assert out["workflowId"] == "wf_test"


# Selection tests by modality/study-type
def test_ct_chest_selects_lung_nodule():
    tools = select_tools("CT", "CT CHEST W/O CONTRAST")
    assert "lung-nodule-detect" in tools

def test_ct_head_selects_ich():
    tools = select_tools("CT", "CT HEAD W/O CONTRAST")
    assert "ich-detect" in tools

def test_ct_aorta_selects_dissection():
    tools = select_tools("CT", "CT AORTA W CONTRAST")
    assert "aortic-dissection-detect" in tools

def test_mr_brain_selects_tumor_screen():
    tools = select_tools("MR", "MR BRAIN W/O CONTRAST")
    assert "brain-tumor-screen" in tools

def test_cr_chest_selects_cxr_screen():
    tools = select_tools("CR", "CHEST AP")
    assert "cxr-screen" in tools

def test_mg_selects_mammo_screen():
    tools = select_tools("MG", "MAMMOGRAM BILATERAL")
    assert "mammo-screen" in tools

def test_unknown_modality_returns_empty():
    tools = select_tools("XY", "UNKNOWN STUDY")
    assert tools == []

def test_ct_multi_region_collects_all_matching_regions():
    tools = select_tools("CT", "CT CHEST ABDOMEN PELVIS")
    assert tools == ["lung-nodule-detect", "pe-detect", "liver-lesion-detect"]

def test_ct_no_region_match_falls_back_to_star():
    tools = select_tools("CT", "CT MISC PROTOCOL")
    assert tools == ["generic-ct-screen"]


# Real first slice (#27): pneumothorax-detect cross-checks the referral reason code.
CXR_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_cxr_test",
    "study": {
        "studyInstanceUID": "1.2.3.4",
        "orthancStudyId": "cxr-study-001",
        "modality": "CR",
        "studyDescription": "CHEST AP",
    },
    "patient": {"fhirPatientId": "Patient/2"},
    "order": {"priority": "stat", "reasonCode": ["J93.1"]},
    "meta": {"traceId": "trc_y", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_pneumothorax_reason_code_records_evidence_but_stays_stubbed():
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    # STUBBED, not COMPLETE: a referral reason code is not pixel-level evidence, and COMPLETE
    # gates the pre-sign fhir2 chart write -- see the comment in handler.py.
    assert ptx["status"] == "STUBBED"
    assert "J93.1" in ptx["label"]
    assert ptx["evidenceRef"] == "order.reasonCode=J93.1"
    assert out["overallStatus"] == "STUBBED"


async def test_pneumothorax_without_matching_reason_code_stays_stubbed():
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R05"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"
    assert ptx["evidenceRef"] is None
    assert out["overallStatus"] == "STUBBED"


async def test_no_reason_code_at_all_stays_stubbed():
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat"}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"


async def test_postprocedural_pneumothorax_code_records_evidence():
    # J95.811 (postprocedural pneumothorax, e.g. r/o PTX post-line film) added per lead review.
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["J95.811"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(f for f in out["findings"] if f["toolId"] == "pneumothorax-detect")
    assert ptx["status"] == "STUBBED"
    assert ptx["evidenceRef"] == "order.reasonCode=J95.811"


async def test_tools_selected_version_reflects_referral_rule_hit():
    out = await handle("interpretation.runTools", {"studyContext": CXR_CONTEXT})
    ptx = next(t for t in out["toolsSelected"] if t["toolId"] == "pneumothorax-detect")
    assert ptx["version"] == "referral-rule-1"
    ctx = {**CXR_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R05"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    ptx = next(t for t in out["toolsSelected"] if t["toolId"] == "pneumothorax-detect")
    assert ptx["version"] == "stub-0"


# Second real slice (#27): pe-detect cross-checks the referral reason code, table-driven off the
# same _reason_finding rule as pneumothorax-detect (per lead review).
# NOTE: studyDescription is "CT CHEST" rather than the real-world "CTPA" protocol name on purpose
# -- the registry's alias/word-boundary matching that would resolve "CTPA" to the chest region
# lives on a separate, not-yet-merged branch (#63). Keep that dependency out of this branch's
# tests; once both merge, "CTPA" will resolve the same way.
CTPA_CONTEXT = {
    "schemaVersion": "1.0.0",
    "workflowId": "wf_ctpa_test",
    "study": {
        "studyInstanceUID": "1.2.3.5",
        "orthancStudyId": "ctpa-study-001",
        "modality": "CT",
        "studyDescription": "CT CHEST W CONTRAST",
    },
    "patient": {"fhirPatientId": "Patient/3"},
    "order": {"priority": "stat", "reasonCode": ["I26.99"]},
    "meta": {"traceId": "trc_z", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}


async def test_pe_reason_code_records_evidence_but_stays_stubbed():
    out = await handle("interpretation.runTools", {"studyContext": CTPA_CONTEXT})
    validate_skill_output("interpretation.runTools", out)
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["status"] == "STUBBED"
    assert "I26.99" in pe["label"]
    assert pe["evidenceRef"] == "order.reasonCode=I26.99"
    assert out["overallStatus"] == "STUBBED"


async def test_pe_with_acute_cor_pulmonale_code_records_evidence():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["I26.02"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=I26.02"


async def test_pe_obstetric_thromboembolism_code_records_evidence():
    # O88.2x (obstetric thromboembolism) sits outside I26 for PE in pregnancy/puerperium --
    # added per lead review.
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["O88.212"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["evidenceRef"] == "order.reasonCode=O88.212"


async def test_pe_without_matching_reason_code_stays_stubbed():
    ctx = {**CTPA_CONTEXT, "order": {"priority": "stat", "reasonCode": ["R91.8"]}}
    out = await handle("interpretation.runTools", {"studyContext": ctx})
    pe = next(f for f in out["findings"] if f["toolId"] == "pe-detect")
    assert pe["status"] == "STUBBED"
    assert pe["evidenceRef"] is None
    assert out["overallStatus"] == "STUBBED"
