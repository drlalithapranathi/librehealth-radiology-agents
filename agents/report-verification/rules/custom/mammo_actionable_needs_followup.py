"""Custom rule: an actionable BI-RADS assessment must carry a follow-up.

BI-RADS 0 (recall), 4 (suspicious) and 5 (highly suggestive of malignancy) are
actionable: the report must record a recommendation (recall / biopsy / short
interval). Fires when one of those assessments is present but no recommendation
was recorded.

Seeded from EMBED BI-RADS semantics (docs/embed-mammography-mapping.md, 5.1).
Gated on a BI-RADS assessment being present, so it never fires on a
non-mammography study. Owner: Saptarshi.
"""
from __future__ import annotations

_ACTIONABLE = {0, 4, 5}


def check(ctx: dict) -> dict | None:
    body = (ctx.get("report") or {}).get("body") or {}
    birads = body.get("biradsAssessment")
    if birads is None:  # not a mammography read we can assess (note: 0 is a real, actionable value)
        return None
    if birads not in _ACTIONABLE:
        return None
    recommendations = (ctx.get("impression") or {}).get("recommendations") or []
    if recommendations:
        return None
    return {
        "ruleId": "mammo-actionable-needs-followup",
        "severity": "WARN",
        "message": f"BI-RADS {birads} is actionable but no follow-up recommendation was recorded.",
        "location": "impression.recommendations",
    }
