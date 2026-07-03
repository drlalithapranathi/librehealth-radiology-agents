"""StudyWorkflow — one instance per study. The durable state machine from ARCHITECTURE.md

Determinism rules (Temporal): no I/O, no wall-clock, no randomness in this file.
- All I/O is in activities (orchestrator/activities.py), called by name.
- Use workflow.now() for time, workflow.wait_condition() for human-gated waits.

Signals drive the two human gates:
- report_finalized      -> leaves AWAITING_RADIOLOGIST (RIS poller sends this)
- signoff_acknowledged  -> leaves AWAITING_SIGNOFF (radiologist addendum/ack)
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

from .state import (
    State,
    ACT_CALL_AGENT,
    ACT_PUBLISH_PRIORITY,
    ACT_ESCALATE,
)

# Tunables (could be moved to a config activity later).
PRE_READ_TIMEOUT = timedelta(minutes=5)
SKILL_TIMEOUT = timedelta(minutes=10)
# Sign-off gate timeout, tier-dependent (#23): STAT reads escalate fastest, ROUTINE slowest.
SIGNOFF_GATE_TIMEOUTS = {
    "STAT": timedelta(hours=1),
    "URGENT": timedelta(hours=2),
    "ROUTINE": timedelta(hours=4),
}
SIGNOFF_GATE_TIMEOUT_DEFAULT = timedelta(hours=4)  # unknown/missing tier -> most lenient


def signoff_timeout_for(tier: str | None) -> timedelta:
    """Tier-dependent sign-off gate timeout (#23). Unknown/missing tier -> the lenient default."""
    return SIGNOFF_GATE_TIMEOUTS.get(tier or "", SIGNOFF_GATE_TIMEOUT_DEFAULT)


@workflow.defn
class StudyWorkflow:
    def __init__(self) -> None:
        self._state: State = State.RECEIVED
        self._report_event: Optional[dict] = None
        self._signoff_ack: Optional[dict] = None
        # Derived results (pass-forward; see WorkflowState in state.py).
        self._triage: Optional[dict] = None
        self._ehr: Optional[dict] = None
        self._ai: Optional[dict] = None
        self._impression: Optional[dict] = None
        self._verification: Optional[dict] = None

    # ---- helpers -----------------------------------------------------------------
    async def _call(self, agent: str, skill_id: str, payload: dict) -> dict:
        return await workflow.execute_activity(
            ACT_CALL_AGENT,
            args=[agent, skill_id, payload],
            start_to_close_timeout=SKILL_TIMEOUT,
        )

    def _base_payload(self, ctx: dict, **derived: Any) -> dict:
        """Skill input = StudyContext + the derived slice this skill depends on."""
        return {"studyContext": ctx, **derived}

    # ---- main run ----------------------------------------------------------------
    @workflow.run
    async def run(self, study_context: dict) -> dict:
        ctx = study_context
        wf_id = ctx["workflowId"]
        study_uid = ctx["study"]["studyInstanceUID"]

        # --- RECEIVED: parallel pre-read fan-out (triage ‖ ehr ‖ ai) --------------
        self._state = State.RECEIVED
        triage_h = self._call("worklist-triage", "triage.score", self._base_payload(ctx))
        ehr_h = self._call("ehr-assistant", "ehr.assembleContext", self._base_payload(ctx))
        ai_h = self._call("interpretation-assistant", "interpretation.runTools", self._base_payload(ctx))
        self._triage, self._ehr, self._ai = await asyncio.gather(triage_h, ehr_h, ai_h)

        # --- READY_FOR_READ: expose priority to the reading worklist --------------
        self._state = State.READY_FOR_READ
        await workflow.execute_activity(
            ACT_PUBLISH_PRIORITY,
            args=[wf_id, study_uid, self._triage],
            start_to_close_timeout=PRE_READ_TIMEOUT,
        )

        # --- AWAITING_RADIOLOGIST: block until the RIS report is finalized --------
        self._state = State.AWAITING_RADIOLOGIST
        await workflow.wait_condition(lambda: self._report_event is not None)
        self._report = self._report_event  # type: ignore[attr-defined]

        # --- IMPRESSION (v1: post-sign safety-net / structuring) ------------------
        self._state = State.IMPRESSION
        self._impression = await self._call(
            "impression-generation",
            "impression.generate",
            self._base_payload(
                ctx,
                report=self._report_event,
                ehrContext=self._ehr,
                aiFindings=self._ai,
            ),
        )

        # --- VERIFY: loop until PASS or a human ack resolves it -------------------
        while True:
            self._state = State.VERIFY
            self._verification = await self._call(
                "report-verification",
                "report.verify",
                self._base_payload(
                    ctx,
                    report=self._report_event,
                    impression=self._impression,
                    ehrContext=self._ehr,
                    aiFindings=self._ai,
                ),
            )
            status = self._verification.get("verificationStatus")
            if status == "PASS" or not self._verification.get("requiresHumanReview"):
                break

            # AWAITING_SIGNOFF: wait for radiologist addendum/ack, else escalate.
            self._state = State.AWAITING_SIGNOFF
            self._signoff_ack = None
            try:
                await workflow.wait_condition(
                    lambda: self._signoff_ack is not None,
                    timeout=signoff_timeout_for((self._triage or {}).get("priorityTier")),
                )
            except asyncio.TimeoutError:
                # The orchestrator owns the durable escalation clock (the real comms agent has no
                # self-firing timer of its own), so on timeout WE page the on-call exactly once via
                # escalate_activity -> comms.dispatch. Best-effort: bounded retries for a transient
                # comms blip, but a persistent outage must NOT strand the durable gate -- we log and
                # let the loop re-escalate on the next tier timeout rather than failing the workflow.
                try:
                    await workflow.execute_activity(
                        ACT_ESCALATE,
                        args=[wf_id, "sign-off gate timed out awaiting radiologist"],
                        start_to_close_timeout=SKILL_TIMEOUT,
                        retry_policy=RetryPolicy(maximum_attempts=3),
                    )
                except Exception:  # noqa: BLE001 - paging is best-effort; never strand the gate
                    workflow.logger.warning(
                        "escalation paging failed for %s; re-escalating on next tier timeout", wf_id
                    )
                # Loop re-verifies; in M2 an addendum updates self._report_event first.

        # --- COMMUNICATE: hand off to the existing Communications Agent (A2A) -----
        self._state = State.COMMUNICATE
        comms = await self._call(
            "communications",
            "comms.dispatch",
            self._base_payload(
                ctx,
                report=self._report_event,
                impression=self._impression,
                verification=self._verification,
            ),
        )

        self._state = State.ARCHIVED
        return {
            "workflowId": wf_id,
            "finalState": self._state.value,
            "triage": self._triage,
            "verification": self._verification,
            "comms": comms,
        }

    # ---- signals & queries -------------------------------------------------------
    @workflow.signal
    def report_finalized(self, event: dict) -> None:
        self._report_event = event

    @workflow.signal
    def signoff_acknowledged(self, ack: dict) -> None:
        self._signoff_ack = ack

    @workflow.query
    def current_state(self) -> str:
        return self._state.value
