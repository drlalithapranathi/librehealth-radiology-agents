"""Locate the /contracts directory regardless of where a service is launched from."""
from __future__ import annotations
import os
from pathlib import Path


def repo_root() -> Path:
    # Allow override in containers; otherwise walk up to find the `contracts` dir.
    env = os.environ.get("LHRAD_REPO_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "contracts").is_dir():
            return parent
    raise RuntimeError("Could not locate repo root containing contracts/. Set LHRAD_REPO_ROOT.")


def contracts_dir() -> Path:
    return repo_root() / "contracts"


def skill_schema(skill_id: str) -> Path:
    # skill_id like "triage.score" -> contracts/skills/triage.schema.json
    domain = skill_id.split(".", 1)[0]
    return contracts_dir() / "skills" / f"{domain}.schema.json"


def card_path(agent_dir_name: str) -> Path:
    return contracts_dir() / "cards" / f"{agent_dir_name}.json"
