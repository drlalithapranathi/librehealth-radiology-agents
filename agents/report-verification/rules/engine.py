"""Declarative YAML rule engine for report verification.

Authoring model (ARCHITECTURE.md): Saptarshi (PI) writes rules in rules/*.yaml without
touching Python. Complex rules go in rules/custom/<id>.py exposing `check(ctx) -> dict|None`
returning an Issue dict {ruleId, severity, message, location} or None.

A rule's `when` clause describes the PROBLEM condition: if it evaluates True, an Issue
is emitted. Paths are dotted and may index lists, e.g. "impression.structuredFindings.0.laterality".
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

_MISSING = object()


@dataclass
class Rule:
    id: str
    severity: str            # INFO | WARN | FAIL
    when: dict
    message: str
    location: str = ""


def load_yaml_rules(rules_dir: Path) -> list[Rule]:
    rules: list[Rule] = []
    for path in sorted(rules_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text())
        rules.append(Rule(
            id=data["id"], severity=data.get("severity", "WARN"),
            when=data.get("when", {}), message=data.get("message", data["id"]),
            location=data.get("location", ""),
        ))
    return rules


def resolve(ctx: dict, path: str) -> Any:
    cur: Any = ctx
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg, _MISSING)
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return _MISSING
        if cur is _MISSING:
            return _MISSING
    return cur


def _truthy_problem(when: dict, ctx: dict) -> bool:
    op = when.get("op")
    left = resolve(ctx, when["field"]) if "field" in when else _MISSING
    right = resolve(ctx, when["ref"]) if "ref" in when else when.get("value", _MISSING)

    if op == "exists":      return left is not _MISSING
    if op == "not_exists":  return left is _MISSING
    if op == "empty":       return left is _MISSING or left in ([], "", {}, None)
    if op == "non_empty":   return left is not _MISSING and bool(left)
    if left is _MISSING or right is _MISSING:
        return False  # cannot compare missing values -> rule does not fire
    if op == "equals":      return left == right
    if op == "not_equals":  return left != right
    if op == "contains":    return right in left
    if op == "gt":          return left > right
    if op == "lt":          return left < right
    return False


def evaluate(rule: Rule, ctx: dict) -> dict | None:
    if _truthy_problem(rule.when, ctx):
        msg = rule.message
        # best-effort interpolation of {field}/{ref} resolved values
        if "field" in rule.when:
            msg = msg.replace("{field}", str(resolve(ctx, rule.when["field"])))
        if "ref" in rule.when:
            msg = msg.replace("{ref}", str(resolve(ctx, rule.when["ref"])))
        return {"ruleId": rule.id, "severity": rule.severity, "message": msg, "location": rule.location}
    return None


def load_custom_checks(custom_dir: Path):
    checks = []
    for path in sorted(custom_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"custom_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        if hasattr(mod, "check"):
            checks.append(mod.check)
    return checks


def run_rules(ctx: dict, rules_dir: Path) -> tuple[str, bool, list[dict]]:
    issues: list[dict] = []
    for rule in load_yaml_rules(rules_dir):
        issue = evaluate(rule, ctx)
        if issue:
            issues.append(issue)
    for check in load_custom_checks(rules_dir / "custom"):
        issue = check(ctx)
        if issue:
            issues.append(issue)

    severities = {i["severity"] for i in issues}
    status = "FAIL" if "FAIL" in severities else "WARN" if "WARN" in severities else "PASS"
    requires_human_review = status in ("WARN", "FAIL")
    return status, requires_human_review, issues
