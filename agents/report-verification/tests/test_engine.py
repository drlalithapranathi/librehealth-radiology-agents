"""Focused tests for declarative rule comparisons."""
import pytest

from rules.engine import Rule, evaluate


def _comparison_rule(op: str, value: object) -> Rule:
    return Rule(
        id=f"test-{op}",
        severity="WARN",
        when={"field": "report.value", "op": op, "value": value},
        message="Comparison matched.",
    )


@pytest.mark.parametrize(
    ("op", "field_value", "rule_value"),
    [
        ("contains", 42, "finding"),
        ("gt", "unexpected text", 5),
        ("lt", [1, 2], 5),
    ],
)
def test_mismatched_comparison_types_do_not_fire(op, field_value, rule_value):
    rule = _comparison_rule(op, rule_value)

    assert evaluate(rule, {"report": {"value": field_value}}) is None


@pytest.mark.parametrize(
    ("op", "field_value", "rule_value"),
    [
        ("contains", "critical finding", "finding"),
        ("gt", 6, 5),
        ("lt", 4, 5),
    ],
)
def test_compatible_comparisons_still_fire(op, field_value, rule_value):
    rule = _comparison_rule(op, rule_value)

    assert evaluate(rule, {"report": {"value": field_value}}) is not None
