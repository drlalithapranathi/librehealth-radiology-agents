"""Tool registry — maps study type to AI tools. v1 returns names only (stubs)."""
from __future__ import annotations

_REGISTRY: dict[str, dict[str, list[str]]] = {
    "CT": {
        "chest":   ["lung-nodule-detect", "pe-detect"],
        "head":    ["ich-detect", "stroke-detect"],
        "abdomen": ["liver-lesion-detect"],
        "spine":   ["vertebral-fracture-detect"],
        "aorta":   ["aortic-dissection-detect"],
        "*":       ["generic-ct-screen"],
    },
    "MR": {
        "brain":   ["brain-tumor-screen", "ms-lesion-detect"],
        "spine":   ["cord-compression-detect"],
        "breast":  ["breast-mri-screen"],
        "*":       ["generic-mr-screen"],
    },
    "CR": {
        "chest":   ["cxr-screen", "pneumothorax-detect"],
        "*":       ["generic-xr-screen"],
    },
    "DX": {
        "chest":   ["cxr-screen", "pneumothorax-detect"],
        "*":       ["generic-xr-screen"],
    },
    "MG": {
        "*":       ["mammo-screen"],
    },
    "US": {
        "abdomen": ["gallstone-detect"],
        "*":       ["generic-us-screen"],
    },
}


def select_tools(modality: str, description: str) -> list[str]:
    """Return the tool list for a given modality and study description."""
    desc = (description or "").lower()
    by_mod = _REGISTRY.get(modality, {})
    for key, tools in by_mod.items():
        if key != "*" and key in desc:
            return tools
    return by_mod.get("*", [])