"""Tool registry — maps study type to AI tools. v1 returns names only (stubs)."""
from __future__ import annotations

_REGISTRY = {
    "CT": {"chest": ["lung-nodule-detect"], "head": ["ich-detect"], "*": ["generic-ct-screen"]},
    "MR": {"*": ["generic-mr-screen"]},
    "CR": {"chest": ["cxr-screen"], "*": ["generic-xr-screen"]},
    "DX": {"chest": ["cxr-screen"], "*": ["generic-xr-screen"]},
}


def select_tools(modality: str, description: str) -> list[str]:
    desc = (description or "").lower()
    by_mod = _REGISTRY.get(modality, {})
    for key, tools in by_mod.items():
        if key != "*" and key in desc:
            return tools
    return by_mod.get("*", [])
