"""Custom rule: a critical finding described in the report body must be flagged.

Post-sign safety-net: if the parsed report narrative names a critical finding but the impression
carries no criticalFlags, the urgent-communication path (critical-comm-required) never triggers.
This WARN surfaces the gap for human review. Word-boundary match so 'mass' does not fire on
'massive'; a known limit is that it does not model negation ('no pneumothorax'), which the M3 LLM
draft handles -- WARN (not FAIL) keeps that miss advisory. Owner: Saptarshi.
"""
from __future__ import annotations
import re

# Verification's own critical vocabulary, kept local -- Golden rule 4 forbids importing another
# agent (Impression Generation keeps its own copy for a different purpose).
_CRITICAL_TERMS = (
    "pneumothorax", "hemorrhage", "haemorrhage", "dissection", "embolism",
    "infarct", "infarction", "occlusion", "perforation", "rupture", "torsion",
)


def check(ctx: dict) -> dict | None:
    body = (ctx.get("report") or {}).get("body") or {}
    if not body.get("present"):
        return None
    if (ctx.get("impression") or {}).get("criticalFlags"):
        return None  # already flagged -> critical-comm-required owns it
    text = (body.get("text") or "").lower()
    hit = next((t for t in _CRITICAL_TERMS if re.search(rf"\b{re.escape(t)}\b", text)), None)
    if not hit:
        return None
    return {
        "ruleId": "critical-finding-unflagged",
        "severity": "WARN",
        "message": f"Report body mentions '{hit}' but the impression carries no critical flag.",
        "location": "impression.criticalFlags",
    }
