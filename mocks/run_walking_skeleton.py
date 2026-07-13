"""Walking skeleton (M0) — runs the whole agent pipeline IN-PROCESS, no Temporal / no A2A
servers required, and validates every hop against /contracts.

This is the M0 "it runs and the contracts hold together" proof. The live wiring
(Temporal workflow + A2A transport) is exercised in M1; here we call each agent's
pure handler directly in the order the StudyWorkflow would.

Run:  python mocks/run_walking_skeleton.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"
sys.path.insert(0, str(ROOT / "libs" / "radagent-common"))

from radagent_common.validation import validate_skill_output  # noqa: E402


def load_handler(agent_dir: str):
    """Import an agent's handler.py despite the hyphenated (non-package) directory."""
    adir = AGENTS / agent_dir
    sys.path.insert(0, str(adir))  # so the handler's sibling imports (registry/rules) resolve
    spec = importlib.util.spec_from_file_location(f"{agent_dir}_handler", adir / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod.handle


class _DemoFhir:
    """Stands in for Fhir2Client so the in-process skeleton exercises Impression's real
    report-content fetch (#16) without a live fhir2. The finalized event is lean (no narrative),
    so the handler fetches the DiagnosticReport `conclusion` from source -- here, this stub."""
    async def get_report_conclusion(self, diagnostic_report_id: str) -> str:
        return "CT chest: large left tension pneumothorax."


async def main() -> int:
    ctx = json.loads((ROOT / "mocks/fixtures/studycontext.sample.json").read_text())
    wf = ctx["workflowId"]
    print(f"\n=== Walking skeleton for {wf} ===\n")

    triage = load_handler("worklist-triage")
    ehr = load_handler("ehr-assistant")
    interp = load_handler("interpretation-assistant")
    impression = load_handler("impression-generation")
    impression.__globals__["_FHIR"] = _DemoFhir()  # inject the fhir2 the handler fetches from (#16)
    verify = load_handler("report-verification")
    verify.__globals__["_FHIR"] = _DemoFhir()  # verify parses report.body from the same conclusion (#22)

    # 1) Pre-read fan-out (triage ‖ ehr ‖ interpretation)
    t, e, a = await asyncio.gather(
        triage("triage.score", {"studyContext": ctx}),
        ehr("ehr.assembleContext", {"studyContext": ctx}),
        interp("interpretation.runTools", {"studyContext": ctx}),
    )
    for skill, out in [("triage.score", t), ("ehr.assembleContext", e), ("interpretation.runTools", a)]:
        validate_skill_output(skill, out)
    print(f"  triage    -> {t['priorityTier']} ({t['priorityScore']})")
    print(f"  ehr       -> priors={len(e['priorStudies'])} labs={len(e['relevantLabs'])}")
    print(f"  interp    -> {out['overallStatus']} tools={len(a['toolsSelected'])}")

    # 2) (radiologist signs report in RIS — simulated finalized event)
    report_event = {"schemaVersion": "1.0.0", "eventType": "ris.report.finalized",
                    "diagnosticReportId": "DiagnosticReport/demo-1", "status": "final",
                    "lastUpdatedCursor": "2026-06-26T12:30:00Z"}

    # 3) Impression
    imp = await impression("impression.generate",
                           {"studyContext": ctx, "report": report_event, "ehrContext": e, "aiFindings": a})
    validate_skill_output("impression.generate", imp)
    print(f"  impression-> {imp['impressionText'][:48]!r}")

    # 4) Verify
    ver = await verify("report.verify",
                       {"studyContext": ctx, "report": report_event, "impression": imp, "ehrContext": e, "aiFindings": a})
    validate_skill_output("report.verify", ver)
    print(f"  verify    -> {ver['verificationStatus']} (human_review={ver['requiresHumanReview']}, issues={len(ver['issues'])})")

    print("\nAll hops validated against /contracts. ✅\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
