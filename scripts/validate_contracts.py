#!/usr/bin/env python3
"""CI gate: every /contracts schema must be a valid Draft 2020-12 schema, every card
must be valid JSON, and every fixture must validate against its schema.

Run: python scripts/validate_contracts.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
errors: list[str] = []


def _load(p: Path) -> dict:
    with p.open() as f:
        return json.load(f)


# 1. All schema files are themselves valid schemas.
schema_files = sorted(CONTRACTS.rglob("*.schema.json"))
for sf in schema_files:
    try:
        Draft202012Validator.check_schema(_load(sf))
    except Exception as e:  # noqa: BLE001
        errors.append(f"[schema invalid] {sf.relative_to(ROOT)}: {e}")

# 2. All agent cards parse as JSON and carry the minimum fields.
for cf in sorted((CONTRACTS / "cards").glob("*.json")):
    try:
        card = _load(cf)
        for key in ("name", "url", "version", "skills"):
            if key not in card:
                errors.append(f"[card missing '{key}'] {cf.relative_to(ROOT)}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"[card invalid json] {cf.relative_to(ROOT)}: {e}")

# 3. Fixtures validate against their schema (extend as fixtures are added).
_studycontext_schema = CONTRACTS / "studycontext.schema.json"
fixture_checks = [
    (ROOT / "mocks/fixtures/studycontext.sample.json", _studycontext_schema),
    (ROOT / "mocks/fixtures/studycontext.ct_aortic_dissection.json", _studycontext_schema),
    (ROOT / "mocks/fixtures/studycontext.cxr_pneumothorax.json", _studycontext_schema),
    (ROOT / "mocks/fixtures/studycontext.mammo_routine.json", _studycontext_schema),
    (ROOT / "mocks/fixtures/studycontext.mr_brain.json", _studycontext_schema),
]
for fixture, schema in fixture_checks:
    if not fixture.exists():
        continue
    v = Draft202012Validator(_load(schema))
    for err in sorted(v.iter_errors(_load(fixture)), key=lambda e: e.path):
        errors.append(f"[fixture invalid] {fixture.name}: {list(err.path)} {err.message}")

# 4. Escalation policy (#29): the YAML matrix validates against its schema -- structurally, and
#    for the cross-field invariants JSON Schema can't express (ordering, single terminal repeat).
_escalation_schema = CONTRACTS / "escalation-policy.schema.json"
_escalation_policy = ROOT / "orchestrator" / "config" / "escalation-policy.yaml"
if _escalation_schema.exists() and _escalation_policy.exists():
    with _escalation_policy.open() as f:
        policy = yaml.safe_load(f)
    v = Draft202012Validator(_load(_escalation_schema))
    struct_errs = sorted(v.iter_errors(policy), key=lambda e: list(e.path))
    for err in struct_errs:
        errors.append(f"[escalation-policy] {list(err.path)} {err.message}")
    if not struct_errs:  # cross-field checks only make sense on a structurally-valid doc
        tiers = policy.get("tiers", {})
        if policy.get("defaultTier") not in tiers:
            errors.append(f"[escalation-policy] defaultTier {policy.get('defaultTier')!r} is not a tier")
        for tier, cfg in tiers.items():
            levels = cfg.get("levels", [])
            nums = [lv["level"] for lv in levels]
            mins = [lv["afterMinutes"] for lv in levels]
            if nums != sorted(nums) or len(set(nums)) != len(nums):
                errors.append(f"[escalation-policy] tier {tier}: level numbers must strictly increase, got {nums}")
            if mins != sorted(mins) or len(set(mins)) != len(mins):
                errors.append(f"[escalation-policy] tier {tier}: afterMinutes must strictly increase, got {mins}")
            repeats = [i for i, lv in enumerate(levels) if lv.get("repeat")]
            if len(repeats) > 1:
                errors.append(f"[escalation-policy] tier {tier}: at most one level may set repeat")
            if repeats and repeats[0] != len(levels) - 1:
                errors.append(f"[escalation-policy] tier {tier}: repeat may only be set on the final level")
            for lv in levels:
                if lv.get("repeat") and "repeatEveryMinutes" not in lv:
                    errors.append(f"[escalation-policy] tier {tier} level {lv['level']}: repeat requires repeatEveryMinutes")

if errors:
    print("CONTRACT VALIDATION FAILED:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print(f"OK: {len(schema_files)} schemas, cards, fixtures, and the escalation policy validated.")
