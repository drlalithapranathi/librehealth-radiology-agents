"""Study -> subspecialty derivation and the out-of-specialty dial (#58).

Behaviour tests run against their own tmp routing file (hermetic; the shipped table is clinical
config a deployment edits). A couple of tests DO read the shipped specialty-routing.yaml -- they
pin that the in-repo file is wired, parseable, and maps the canonical cases, on top of the CI
schema validation in scripts/validate_contracts.py.
"""
import textwrap

import routing
from routing import FALLBACK_ANY_ON_CALL, derive_specialty, out_of_specialty_fallback


def _point_at(monkeypatch, tmp_path, body: str):
    p = tmp_path / "routing.yaml"
    p.write_text(textwrap.dedent(body))
    monkeypatch.setenv("SPECIALTY_ROUTING_PATH", str(p))
    return p


TABLE = """\
    schemaVersion: "1.0.0"
    outOfSpecialtyFallback: any-on-call
    rules:
      - specialty: breast
        modalities: [MG]
      - specialty: neuro
        keywords: [head, brain]
      - specialty: chest
        keywords: [chest]
"""


# --- derivation -----------------------------------------------------------------------

def test_modality_match_is_case_insensitive(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, TABLE)
    assert derive_specialty({"modality": "mg"}) == "breast"


def test_keyword_matches_inside_the_description_case_insensitively(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, TABLE)
    assert derive_specialty({"modality": "CT", "studyDescription": "CT HEAD W/O CONTRAST"}) == "neuro"


def test_first_matching_rule_wins(monkeypatch, tmp_path):
    """'CT head and chest' hits both the neuro and chest rules; file order decides."""
    _point_at(monkeypatch, tmp_path, TABLE)
    assert derive_specialty({"modality": "CT", "studyDescription": "CT head and chest"}) == "neuro"


def test_unmatched_study_gets_no_specialty(monkeypatch, tmp_path):
    """None = unnarrowed on-call search, the pre-#58 behaviour. A single general rota is
    unaffected, and an unmapped study must never have a specialty invented for it."""
    _point_at(monkeypatch, tmp_path, TABLE)
    assert derive_specialty({"modality": "US", "studyDescription": "US thyroid"}) is None
    assert derive_specialty({"modality": "CT"}) is None
    assert derive_specialty({}) is None


# --- the dial -------------------------------------------------------------------------

def test_the_fallback_dial_reads_from_the_table(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, """\
        schemaVersion: "1.0.0"
        outOfSpecialtyFallback: none
        rules:
          - specialty: neuro
            keywords: [head]
    """)
    assert out_of_specialty_fallback() == "none"


def test_an_unknown_dial_value_degrades_to_any_on_call(monkeypatch, tmp_path):
    """The in-repo file is CI-validated; this guards a live edit or SPECIALTY_ROUTING_PATH
    override. Of the two failure directions a typo could buy, the one where someone hears the
    page is the right one."""
    _point_at(monkeypatch, tmp_path, """\
        schemaVersion: "1.0.0"
        outOfSpecialtyFallback: ask-the-pi
        rules:
          - specialty: neuro
            keywords: [head]
    """)
    assert out_of_specialty_fallback() == FALLBACK_ANY_ON_CALL


# --- config disasters must not silence a page -----------------------------------------

def test_a_missing_routing_file_degrades_to_no_specialty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SPECIALTY_ROUTING_PATH", str(tmp_path / "does-not-exist.yaml"))
    assert derive_specialty({"modality": "MG"}) is None
    assert out_of_specialty_fallback() == FALLBACK_ANY_ON_CALL


def test_an_unparseable_routing_file_degrades_the_same_way(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "rules: [unclosed")
    assert derive_specialty({"modality": "MG"}) is None
    assert out_of_specialty_fallback() == FALLBACK_ANY_ON_CALL


def test_a_file_that_parses_but_is_not_a_mapping_degrades_the_same_way(monkeypatch, tmp_path):
    _point_at(monkeypatch, tmp_path, "- just\n- a\n- list\n")
    assert derive_specialty({"modality": "MG"}) is None
    assert out_of_specialty_fallback() == FALLBACK_ANY_ON_CALL


def test_a_malformed_rule_degrades_instead_of_blocking_the_page(monkeypatch, tmp_path):
    """A live-edited rule missing its `specialty` must not turn every dispatch into a crash
    (a failed dispatch retries forever with no page sent). CI validates the in-repo table; this
    guards the SPECIALTY_ROUTING_PATH override that bypasses it."""
    _point_at(monkeypatch, tmp_path, """\
        schemaVersion: "1.0.0"
        outOfSpecialtyFallback: any-on-call
        rules:
          - keywords: [head]
    """)
    assert derive_specialty({"modality": "CT", "studyDescription": "CT head"}) is None


# --- the shipped table ----------------------------------------------------------------

def test_the_shipped_table_loads_and_maps_the_canonical_cases(monkeypatch):
    """No env override: this reads agents/communications/specialty-routing.yaml itself. Loose on
    purpose -- it pins that the file is wired and sane, not every keyword (that list is clinical
    config under PI review, and CI already schema-validates it)."""
    monkeypatch.delenv("SPECIALTY_ROUTING_PATH", raising=False)
    assert routing._routing_path().is_file()
    assert derive_specialty({"modality": "MG"}) == "breast"
    assert derive_specialty({"modality": "CT", "studyDescription": "CT head"}) == "neuro"
    assert out_of_specialty_fallback() in ("any-on-call", "none")
