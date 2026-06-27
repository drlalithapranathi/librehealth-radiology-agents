"""Report Verification handler — engine owner: Pranathi; rules owner: Saptarshi.

Input  : { studyContext, report?, impression?, ehrContext?, aiFindings? }
Output : contracts/skills/report.schema.json
"""
from __future__ import annotations
from pathlib import Path
from radagent_common.tracing import now_iso
from rules.engine import run_rules

AGENT_VERSION = "0.1.0"
_RULES_DIR = Path(__file__).resolve().parent / "rules"


async def handle(skill_id: str, payload: dict) -> dict:
    assert skill_id == "report.verify", f"unexpected skill {skill_id}"
    ctx = payload["studyContext"]
    rule_ctx = {
        "report": payload.get("report") or {},
        "impression": payload.get("impression") or {},
        "ehrContext": payload.get("ehrContext") or {},
        "aiFindings": payload.get("aiFindings") or {},
    }
    status, requires_review, issues = run_rules(rule_ctx, _RULES_DIR)
    return {
        "schemaVersion": "1.0.0",
        "workflowId": ctx["workflowId"],
        "verificationStatus": status,
        "requiresHumanReview": requires_review,
        "issues": issues,
        "agentVersion": AGENT_VERSION,
        "verifiedAt": now_iso(),
    }
