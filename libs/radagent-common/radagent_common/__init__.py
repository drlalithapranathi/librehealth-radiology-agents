"""radagent_common — shared building blocks for the LH-Radiology agent system.

Golden rule for agent authors: your handler is a pure async function dict -> dict.
You never import `a2a.*` directly. All protocol specifics live in `radagent_common.a2a`.
"""
from .context import StudyContext
from .validation import validate_against, ContractError
from . import paths

__all__ = ["StudyContext", "validate_against", "ContractError", "paths"]
