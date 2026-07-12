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
