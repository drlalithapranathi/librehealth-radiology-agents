"""Custom rule: a critical finding described in the report body must be flagged.

Post-sign safety-net: if the parsed report narrative names a critical finding but the impression
carries no criticalFlags, the urgent-communication path (critical-comm-required) never triggers.
This WARN surfaces the gap for human review. Word-boundary (and plural-aware) matching, so a term never fires inside a longer word. Negation-aware (#78): a pertinent-negative narrative ("No pneumothorax, effusion, or
consolidation") no longer fires -- the shared negation window (radagent_common.negation) suppresses
a negated term while keeping a real one ("large pneumothorax"). It stays WARN, not FAIL, so any
residual miss is advisory. Owner: Saptarshi.
"""
from __future__ import annotations

from radagent_common.negation import find_asserted_terms, scannable_text

# Verification's own critical vocabulary, kept local -- Golden rule 4 forbids importing another
# agent (Impression Generation keeps its own copy for a different purpose). The negation LOGIC is
# shared (radagent_common.negation); only the vocabulary is per-agent.
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
    # The shared scannable_text (not report_body's sections dict) scopes the scan: it drops ONLY the
    # provably non-finding sections (indication/history/technique/comparison -- the indication names
    # the SUSPICION, which re-flagged every normal study), while keeping the pre-header preamble and
    # any unknown-headed content (organ sub-headers like "CHEST:") that the sections dict either
    # drops or mis-attributes -- both reproduced misses (#78). No headers -> the whole text.
    text = scannable_text(body.get("text") or "")
    hits = find_asserted_terms(text, _CRITICAL_TERMS)
    if not hits:
        return None
    hit = hits[0]
    return {
        "ruleId": "critical-finding-unflagged",
        "severity": "WARN",
        "message": f"Report body mentions '{hit}' but the impression carries no critical flag.",
        "location": "impression.criticalFlags",
    }
