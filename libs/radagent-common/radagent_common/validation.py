"""Validate skill payloads against the JSON Schemas in /contracts (the source of truth)."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from jsonschema import Draft202012Validator
from . import paths


class ContractError(ValueError):
    """Raised when a payload does not conform to its contract schema."""


def _load(schema_path: Path) -> dict:
    with schema_path.open() as f:
        return json.load(f)


def validate_against(payload: dict[str, Any], schema_path: Path) -> None:
    validator = Draft202012Validator(_load(schema_path))
    errors = sorted(validator.iter_errors(payload), key=lambda e: e.path)
    if errors:
        msgs = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:10])
        raise ContractError(f"{schema_path.name}: {msgs}")


def validate_skill_output(skill_id: str, payload: dict[str, Any]) -> None:
    validate_against(payload, paths.skill_schema(skill_id))
