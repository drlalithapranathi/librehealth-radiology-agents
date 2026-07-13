"""Custom rule: a structured report with findings must carry an impression/conclusion.

Fires WARN when the parsed body has a FINDINGS section but no IMPRESSION (or CONCLUSION) section
-- a report that describes findings without stating a conclusion is incomplete. Gated on the
body actually splitting into sections, so an unstructured or single-line conclusion (no headers)
never trips it. Owner: Saptarshi.
"""
from __future__ import annotations


def check(ctx: dict) -> dict | None:
    body = (ctx.get("report") or {}).get("body") or {}
    if not body.get("present"):
        return None
    sections = body.get("sections") or {}
    # split_sections folds a CONCLUSION header into the `impression` key, so this one test covers both.
    if sections.get("findings") and not sections.get("impression"):
        return {
            "ruleId": "impression-section-present",
            "severity": "WARN",
            "message": "Report has a findings section but no impression/conclusion.",
            "location": "report.body.sections",
        }
    return None
