"""Escalation-ladder wiring (#29): the policy loader, the rung dispatch slice, and the gate's
climb/hold/fallback behavior around it.

The ladder-climb sequence itself is covered in test_workflow_gates.py (with a mocked ladder)
and rung-1 tier parity in test_signoff_timeouts.py (with the real policy); this file covers
the loader's tier resolution, the escalation slice escalate_activity passes forward, the
ack-before-any-rung path, and the legacy fallback when the policy cannot be loaded.
Skipped unless temporalio is installed.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

pytest.importorskip("temporalio", reason="temporalio not installed")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment, ActivityEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.state import TASK_QUEUE  # noqa: E402
from orchestrator.workflow import StudyWorkflow  # noqa: E402

# Validator for the pass-forward escalation slice. $defs/dispatchEscalation $refs sibling $defs, so
# we validate against a wrapper that keeps the whole $defs block in scope.
_ESCALATION_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts" / "escalation-policy.schema.json").read_text()
)
_DISPATCH_ESCALATION_VALIDATOR = Draft202012Validator(
    {"$schema": _ESCALATION_SCHEMA["$schema"], "$defs": _ESCALATION_SCHEMA["$defs"],
     "$ref": "#/$defs/dispatchEscalation"}
)


# --- unit: load_escalation_policy_activity resolves tiers against the real policy ---

def test_loader_returns_the_tiers_ladder():
    ladder = asyncio.run(activities.load_escalation_policy_activity("STAT"))
    assert [r["level"] for r in ladder] == [1, 2, 3]
    assert ladder[0]["afterMinutes"] == 60          # rung 1 mirrors the pre-#29 STAT timeout
    assert ladder[-1]["repeat"] is True


def test_loader_unknown_or_missing_tier_gets_the_default_ladder():
    routine = asyncio.run(activities.load_escalation_policy_activity("ROUTINE"))
    for tier in (None, "", "WEIRD"):
        assert asyncio.run(activities.load_escalation_policy_activity(tier)) == routine
    assert routine[0]["afterMinutes"] == 240        # rung 1 mirrors the pre-#29 lenient default


def test_loader_honors_env_override_per_call(tmp_path, monkeypatch):
    """ESCALATION_POLICY_PATH re-points the policy without a worker restart (read per gate entry)."""
    alt = tmp_path / "policy.yaml"
    alt.write_text(
        "schemaVersion: '1.0.0'\n"
        "defaultTier: ONLY\n"
        "tiers:\n"
        "  ONLY:\n"
        "    levels:\n"
        "      - level: 1\n"
        "        afterMinutes: 5\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n"
    )
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(alt))
    ladder = asyncio.run(activities.load_escalation_policy_activity("STAT"))
    assert ladder == [{"level": 1, "afterMinutes": 5, "targetRole": "department-lead",
                       "channels": ["phone"], "urgency": "critical"}]


def test_loader_missing_file_raises(tmp_path, monkeypatch):
    """A broken deploy surfaces as an activity failure -> the workflow falls back to the
    legacy single-timeout gate (integration below) instead of escalating silently wrong."""
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        asyncio.run(activities.load_escalation_policy_activity("STAT"))


def _write_policy(tmp_path, rung_yaml: str) -> Path:
    p = tmp_path / "policy.yaml"
    p.write_text(
        "schemaVersion: '1.0.0'\n"
        "defaultTier: ONLY\n"
        "tiers:\n"
        "  ONLY:\n"
        "    levels:\n" + rung_yaml
    )
    return p


def test_loader_rejects_a_rung_missing_afterMinutes(tmp_path, monkeypatch):
    """A parseable-but-malformed policy surfaces as an activity failure -> the gate falls back,
    rather than the workflow raising a bare KeyError on rung["afterMinutes"] and hot-retry-wedging
    the study. Guards the live-edit / ESCALATION_POLICY_PATH override paths, which bypass CI."""
    bad = _write_policy(tmp_path,
        "      - level: 1\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n")
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(bad))
    with pytest.raises(ValueError):
        asyncio.run(activities.load_escalation_policy_activity("ONLY"))


def test_loader_rejects_a_repeating_rung_missing_cadence(tmp_path, monkeypatch):
    """A repeating final rung without repeatEveryMinutes would KeyError inside the workflow's
    repeat loop; the loader rejects it up front so the gate falls back instead of wedging."""
    bad = _write_policy(tmp_path,
        "      - level: 1\n"
        "        afterMinutes: 5\n"
        "        targetRole: department-lead\n"
        "        channels: [phone]\n"
        "        urgency: critical\n"
        "        repeat: true\n")
    monkeypatch.setenv("ESCALATION_POLICY_PATH", str(bad))
    with pytest.raises(ValueError):
        asyncio.run(activities.load_escalation_policy_activity("ONLY"))


# --- unit: escalate_activity passes the fired rung forward as the escalation slice ---

def test_escalate_activity_passes_the_rung_slice(monkeypatch):
    """The dispatch carries the rung's who/how/how-loudly (dispatchEscalation slice) and drops
    its scheduling fields; the legacy critical marker is NOT faked alongside it."""
    captured: dict = {}

    async def _fake_dispatch(base_url, skill_id, payload):
        captured.update(skill_id=skill_id, payload=payload)
        return {"schemaVersion": "1.0.0", "workflowId": "wf_rung", "dispatchStatus": "SENT",
                "agentVersion": "0.1.0", "dispatchedAt": "2026-07-11T00:00:00Z"}

    monkeypatch.setattr(activities, "call_agent_skill", _fake_dispatch)
    rung = {"level": 3, "afterMinutes": 120, "targetRole": "department-lead",
            "channels": ["pager", "phone"], "urgency": "critical",
            "repeat": True, "repeatEveryMinutes": 30, "attempt": 4}

    asyncio.run(ActivityEnvironment().run(
        activities.escalate_activity, "wf_rung", "sign-off gate timed out awaiting radiologist", rung))

    assert captured["skill_id"] == "comms.dispatch"
    assert captured["payload"]["escalation"] == {
        "level": 3, "targetRole": "department-lead", "channels": ["pager", "phone"],
        "urgency": "critical", "attempt": 4,
        "reason": "sign-off gate timed out awaiting radiologist",
    }
    assert "verification" not in captured["payload"]    # no faked FAIL when the rung speaks
    assert "afterMinutes" not in captured["payload"]["escalation"]
    # And it must satisfy its contract: the exact-dict above pins today's shape, but validating
    # against $defs/dispatchEscalation catches producer/contract drift ($def is otherwise
    # unreferenced, so validate_contracts.py never exercises it against a real emitted slice).
    _DISPATCH_ESCALATION_VALIDATOR.validate(captured["payload"]["escalation"])


# --- integration plumbing -----------------------------------------------------------

STUDY_CONTEXT = {
    "schemaVersion": "1.0.0", "workflowId": "wf_ladder",
    "study": {"studyInstanceUID": "1.2.3", "orthancStudyId": "abc", "modality": "CT"},
    "patient": {"fhirPatientId": "Patient/1"}, "order": {},
    "meta": {"traceId": "t", "emittedAt": "2026-06-26T00:00:00Z", "source": "test"},
}

_STATE: dict = {}


def _reset() -> None:
    _STATE.clear()
    _STATE["verify_i"] = 0
    _STATE["escalations"] = []


@activity.defn(name="call_agent_skill_activity")
async def _mock_call(agent: str, skill_id: str, payload: dict) -> dict:
    if skill_id == "report.verify":
        _STATE["verify_i"] += 1
        first = _STATE["verify_i"] == 1
        return {"verificationStatus": "FAIL" if first else "PASS",
                "requiresHumanReview": first, "issues": []}
    if skill_id == "triage.score":
        return {"priorityTier": "ROUTINE", "priorityScore": 50}
    return {"ok": True}


@activity.defn(name="publish_priority_activity")
async def _mock_publish(workflow_id: str, study_instance_uid: str, triage: dict) -> None:
    return None


@activity.defn(name="escalate_activity")
async def _mock_escalate(workflow_id: str, reason: str, escalation: dict | None = None) -> None:
    _STATE["escalations"].append((workflow_id, reason, escalation))


@activity.defn(name="load_escalation_policy_activity")
async def _mock_load_policy(tier: str | None) -> list[dict]:
    return [{"level": 1, "afterMinutes": 240, "targetRole": "reading-radiologist",
             "channels": ["in-app"], "urgency": "routine"}]


@activity.defn(name="load_escalation_policy_activity")
async def _boom_load_policy(tier: str | None) -> list[dict]:
    raise RuntimeError("policy store down")


async def _wait_state(handle, target: str, tries: int = 200) -> None:
    for _ in range(tries):
        if await handle.query(StudyWorkflow.current_state) == target:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"workflow never reached {target}")


# --- integration: ack before any rung -> no paging at all ---------------------------

def test_ack_before_first_rung_pages_nobody():
    """The gate is a human gate first: an ack before rung 1 (240m) elapses means zero
    escalations. Time skipping is locked while we query/signal, so rung 1 cannot fire early."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_escalate,
                                          _mock_load_policy]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-ladder-ack", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                await _wait_state(handle, "AWAITING_SIGNOFF")
                await handle.signal(StudyWorkflow.signoff_acknowledged, {"ackBy": "Practitioner/9"})
                result = await handle.result()
        assert result["finalState"] == "ARCHIVED"
        assert _STATE["escalations"] == []
    asyncio.run(scenario())


# --- integration: policy unavailable -> legacy single-timeout gate ------------------

def test_policy_load_failure_falls_back_to_legacy_gate():
    """A config disaster must not silence escalation OR strand the gate: the ladder loader
    fails (bounded retries), the gate falls back to the pre-#29 behavior — single tier
    timeout, one flat page (escalation=None), back to the verify loop — and the study
    completes without an ack, exactly as it did before #29."""
    async def scenario():
        _reset()
        async with await WorkflowEnvironment.start_time_skipping() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[StudyWorkflow],
                              activities=[_mock_call, _mock_publish, _mock_escalate,
                                          _boom_load_policy]):
                handle = await env.client.start_workflow(
                    StudyWorkflow.run, STUDY_CONTEXT, id="wf-ladder-fallback", task_queue=TASK_QUEUE
                )
                await _wait_state(handle, "AWAITING_RADIOLOGIST")
                await handle.signal(StudyWorkflow.report_finalized,
                                    {"diagnosticReportId": "DiagnosticReport/1"})
                result = await handle.result()  # env time-skips the legacy 4h ROUTINE timeout
        assert result["finalState"] == "ARCHIVED"
        assert len(_STATE["escalations"]) == 1
        wf, reason, esc = _STATE["escalations"][0]
        assert (wf, esc) == ("wf_ladder", None)    # the legacy flat page, no rung slice
        assert "sign-off" in reason
    asyncio.run(scenario())
