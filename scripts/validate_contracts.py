#!/usr/bin/env python3
"""CI gate: every /contracts schema must be a valid Draft 2020-12 schema, every card
must be valid JSON, and every fixture must validate against its schema.

Run: python scripts/validate_contracts.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
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
fixture_checks = [
    (ROOT / "mocks/fixtures/studycontext.sample.json", CONTRACTS / "studycontext.schema.json"),
]
for fixture, schema in fixture_checks:
    if not fixture.exists():
        continue
    v = Draft202012Validator(_load(schema))
    for err in sorted(v.iter_errors(_load(fixture)), key=lambda e: e.path):
        errors.append(f"[fixture invalid] {fixture.name}: {list(err.path)} {err.message}")

if errors:
    print("CONTRACT VALIDATION FAILED:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print(f"OK: {len(schema_files)} schemas, cards, and fixtures validated.")
