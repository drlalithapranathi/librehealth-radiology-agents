"""Tool registry — maps study type to AI tools. v1 returns names only (stubs)."""
from __future__ import annotations
import re

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

# How radiologists actually name the region a key stands for (#63).
#
# The registry keys are anatomical ("chest", "head"), but a DICOM StudyDescription carries the
# PROTOCOL name the department typed -- "CTPA", "CXR", "CT BRAIN". Matching the key alone means a
# study whose description never spells out the anatomy falls through to the modality's generic
# screen: `CT BRAIN` and `NCCT BRAIN` missed `ich-detect`, `MRI HEAD` missed the brain tools, `CXR`
# missed `pneumothorax-detect`, and `CTPA` missed the very tool named after it. Half the real
# descriptions we can name selected the wrong tool set.
#
# Keyed by REGION, not by modality, on purpose: CT calls it "head" and MR calls it "brain", so each
# is the other's alias and a `CT BRAIN` / `MRI HEAD` both land correctly.
#
# Only regions the registry ALREADY has are listed. Adding NEW modalities and regions (PT, NM, XA,
# US thyroid, ...) is coverage work and belongs to #45; this is a selection-correctness fix, and
# keeping `_REGISTRY` itself byte-for-byte unchanged means #45's data expansion merges cleanly on
# top of it.
_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "chest":   ("cxr", "ctpa", "thorax", "lung", "lungs", "pulmonary"),
    "head":    ("brain", "cerebral", "cranial", "circle of willis"),
    "brain":   ("head", "cerebral", "cranial"),
    "abdomen": ("abd", "ruq", "luq", "liver", "hepatic", "gallbladder", "biliary"),
    "spine":   ("lumbar", "cervical", "vertebral"),
    "aorta":   ("aortic",),
}

# Aliases match on WORD BOUNDARIES, unlike the plain-substring match on the key itself.
#
# They have to. An alias is short and clinical where a key is anatomical, so it turns up inside
# unrelated words: "liver" sits inside "deLIVERy", which means a plain substring match hands an
# obstetric `US OB DELIVERY PLANNING` the abdomen region and runs `gallstone-detect` on it. Widening
# the match must not also loosen it -- guarded in tests/test_handler.py.
#
# Related: "thoracic" is deliberately NOT an alias of chest, because a `CT THORACIC SPINE` is a spine
# study. ("thorax" is safe -- it is not a substring of "thoracic".)
#
# The boundary costs nothing on real descriptions: `CT L-SPINE` and `MRI C-SPINE` still match,
# because a hyphen is a word boundary too.
_ALIAS_RE: dict[str, re.Pattern[str]] = {
    region: re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b")
    for region, aliases in _REGION_ALIASES.items()
}


def _matches_region(desc: str, key: str) -> bool:
    """Does this study description name the given body region — under any of its names?

    Strictly ADDITIVE over the old `key in desc` test: every description that matched a region
    before still matches it. So this can only ever hand a study MORE regional tools, never fewer,
    and no study that was already selecting correctly can regress onto the generic screen.
    """
    if key in desc:
        return True
    pattern = _ALIAS_RE.get(key)
    return bool(pattern and pattern.search(desc))


def select_tools(modality: str, description: str) -> list[str]:
    """Return the tool list for a given modality and study description.

    Collects tools from every matching body-part key (deduped, in registry
    order) so multi-region studies run all applicable regional tools, not
    just the first match. A region matches on its key or any of its aliases
    (#63). Falls back to "*" when no body-part key matches.
    """
    desc = (description or "").lower()
    by_mod = _REGISTRY.get(modality, {})
    matched: list[str] = []
    for key, tools in by_mod.items():
        if key != "*" and _matches_region(desc, key):
            for tool in tools:
                if tool not in matched:
                    matched.append(tool)
    if matched:
        return matched
    return by_mod.get("*", [])
