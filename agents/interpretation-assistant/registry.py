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
    """Return the tool list for a given modality and study description.

    Collects tools from every matching body-part key (deduped, in registry
    order) so multi-region studies run all applicable regional tools, not
    just the first match. Falls back to "*" when no body-part key matches.
    """
    desc = (description or "").lower()
    by_mod = _REGISTRY.get(modality, {})
    matched: list[str] = []
    for key, tools in by_mod.items():
        if key != "*" and key in desc:
            for tool in tools:
                if tool not in matched:
                    matched.append(tool)
    if matched:
        return matched
    return by_mod.get("*", [])
