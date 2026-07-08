"""Custom rule: a mammography read must state breast density.

Breast density (ACR A-D) is a required element of a mammography report and a
Verification concordance input. Fires when a BI-RADS assessment is present (so
this is a mammography read) but no breast-density statement was recorded.

Seeded from EMBED tissueden semantics (docs/embed-mammography-mapping.md, 5.2).
Gated on a BI-RADS assessment being present, so it never fires on a
non-mammography study. Owner: Saptarshi.
"""
from __future__ import annotations


def check(ctx: dict) -> dict | None:
    body = (ctx.get("report") or {}).get("body") or {}
    if body.get("biradsAssessment") is None:  # not a mammography read
        return None
    density = body.get("breastDensity")
    if density:  # any non-empty A-D statement satisfies the rule
        return None
    return {
        "ruleId": "mammo-density-stated",
        "severity": "WARN",
        "message": "Mammography read is missing a breast-density (ACR A-D) statement.",
        "location": "report.body.breastDensity",
    }
