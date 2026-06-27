"""Example custom (Python) rule — for logic too complex for the YAML DSL.

Return an Issue dict or None. Owner: Saptarshi.
"""
from __future__ import annotations


def check(ctx: dict) -> dict | None:
    impression = ctx.get("impression") or {}
    text = (impression.get("impressionText") or "").lower()
    recs = impression.get("recommendations") or []
    # Example: a 'nodule' mention should carry a follow-up recommendation.
    if "nodule" in text and not recs:
        return {
            "ruleId": "nodule-followup-missing",
            "severity": "WARN",
            "message": "Nodule mentioned without a follow-up recommendation.",
            "location": "impression.recommendations",
        }
    return None
