"""Unit tests for the report-body parser (issue #22)."""
from rules.report_body import (
    detect_laterality, parse_birads, parse_breast_density, parse_report_body, split_sections,
)


def test_detect_laterality():
    assert detect_laterality("Left breast mass.") == "left"
    assert detect_laterality("Right lower lobe opacity.") == "right"
    assert detect_laterality("Bilateral pleural effusions.") == "bilateral"
    assert detect_laterality("Left and right kidneys normal.") == "bilateral"
    assert detect_laterality("No side stated.") is None
    assert detect_laterality("") is None


def test_split_sections():
    text = "TECHNIQUE: PA and lateral.\nFINDINGS: Clear lungs.\nIMPRESSION: Normal chest."
    s = split_sections(text)
    assert s["technique"] == "PA and lateral."
    assert s["findings"] == "Clear lungs."
    assert s["impression"] == "Normal chest."


def test_conclusion_header_folds_into_impression():
    assert split_sections("CONCLUSION: No acute disease.")["impression"] == "No acute disease."


def test_no_headers_yields_no_sections():
    assert split_sections("Just a single sentence with no headers.") == {}


def test_parse_birads_including_out_of_range():
    assert parse_birads("BI-RADS 4") == 4
    assert parse_birads("BIRADS category 0") == 0
    assert parse_birads("ACR BI-RADS: 4a") == 4
    assert parse_birads("ACR BI-RADS 7") == 7  # out-of-range value preserved for the validity rule
    assert parse_birads("no assessment") is None


def test_parse_breast_density():
    assert parse_breast_density("ACR density C") == "C"
    assert parse_breast_density("Breast density: b") == "B"
    assert parse_breast_density("ACR (D)") == "D"
    assert parse_breast_density("No density stated.") is None


def test_parse_report_body_present_and_fields():
    empty = parse_report_body("   ")
    assert empty["present"] is False and empty["laterality"] is None

    body = parse_report_body("IMPRESSION: BI-RADS 4 mass, left breast. ACR density B.")
    assert body["present"] is True
    assert body["laterality"] == "left"
    assert body["biradsAssessment"] == 4
    assert body["breastDensity"] == "B"
    assert body["sections"]["impression"].startswith("BI-RADS 4")
