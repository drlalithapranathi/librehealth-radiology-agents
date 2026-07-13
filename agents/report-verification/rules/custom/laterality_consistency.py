"""Custom rule: report-body laterality must not contradict the impression's laterality.

Fires WARN only when BOTH the parsed report body and the impression name a DEFINITE, OPPOSITE
side (left vs right) -- a wrong-side error is a sentinel event. Silent when either side is
absent or 'bilateral', so an unstated laterality never raises a false alarm.

Replaces the v1 laterality-consistency.yaml, whose `ref` (impression.structuredFindings.0.
laterality) is never populated -- the impression agent emits {label, severity} findings with no
laterality -- so the YAML rule could not fire. The engine now derives the impression laterality
from its text (impression.derivedLaterality); this rule compares the two parsed sides.
Owner: Saptarshi.
"""
from __future__ import annotations

_DEFINITE = {"left", "right"}


def check(ctx: dict) -> dict | None:
    body = (ctx.get("report") or {}).get("body") or {}
    if not body.get("present"):
        return None
    body_lat = body.get("laterality")
    imp_lat = (ctx.get("impression") or {}).get("derivedLaterality")
    if body_lat in _DEFINITE and imp_lat in _DEFINITE and body_lat != imp_lat:
        return {
            "ruleId": "laterality-consistency",
            "severity": "WARN",
            "message": f"Laterality mismatch: report body says {body_lat} but impression says {imp_lat}.",
            "location": "impression",
        }
    return None
