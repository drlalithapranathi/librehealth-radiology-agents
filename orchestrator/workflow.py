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
from temporalio.exceptions import ActivityError, ApplicationError

from .state import (
    State,
    ACT_CALL_AGENT,
    ACT_START_AGENT,
    ACT_PUBLISH_PRIORITY,
    ACT_ESCALATE,
    ACT_LOAD_ESCALATION_POLICY,
)

# Tunables (could be moved to a config activity later).
PRE_READ_TIMEOUT = timedelta(minutes=5)
SKILL_TIMEOUT = timedelta(minutes=10)
# LEGACY sign-off gate timeouts, tier-dependent (#23): STAT reads escalate fastest, ROUTINE
# slowest. Since #29 the gate normally climbs the escalation ladder from
# orchestrator/config/escalation-policy.yaml (whose rung 1 mirrors these values); this map is the
# fallback when that policy cannot be loaded -- a config disaster must not silence escalation.
SIGNOFF_GATE_TIMEOUTS = {
    "STAT": timedelta(hours=1),
    "URGENT": timedelta(hours=2),
    "ROUTINE": timedelta(hours=4),
}
SIGNOFF_GATE_TIMEOUT_DEFAULT = timedelta(hours=4)  # unknown/missing tier -> most lenient
# Backstop on a repeating final rung (#29): stop re-paging after this many fires of that rung so
# an abandoned study cannot grow workflow history without bound. A history-size guard, NOT policy
# (cadence/audience live in escalation-policy.yaml); the gate itself keeps holding for the ack.
ESCALATION_REPEAT_CAP = 50
# Push-mode skill re-runs (#24): the SDK's push sender POSTs each callback exactly once (no
# retry), so a lost callback times out the wait and we re-run the whole skill — bounded, because
# each re-run starts a fresh (non-idempotent) agent task. Mirrors the unary path, where Temporal
# re-runs the activity; there the default policy is unbounded, here duplicates have a cost.
PUSH_SKILL_ATTEMPTS = 3
# Backstop bound on buffered push results (duplicate/orphaned taskIds an activity retry can
# strand): dicts iterate in insertion order, so evicting the oldest entry is replay-deterministic.
PUSH_RESULT_CAP = 32
# Shared activity-retry config (#29): the non-idempotent activities -- starting a push skill and
# firing an escalation page each mint a fresh side effect on every attempt -- and the policy loader
# (a deterministic failure that a retry won't fix) share ONE bounded policy instead of an ad-hoc
# RetryPolicy(maximum_attempts=3) repeated per call site. The idempotent read-through calls (_call
# agent skills, publish_priority) deliberately keep Temporal's default UNBOUNDED retry, so a
# transient outage self-heals rather than failing a study that cannot proceed without the result.
BOUNDED_ACTIVITY_RETRY = RetryPolicy(maximum_attempts=3)


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
        # Push-notification results, keyed by A2A taskId (#24). Filled by the skill_completed
        # signal (ingress relays the agent's callback); consumed by _call_push.
        self._skill_results: dict[str, dict] = {}

    # ---- helpers -----------------------------------------------------------------
    async def _call(self, agent: str, skill_id: str, payload: dict) -> dict:
        # Idempotent read-through -> Temporal's default unbounded retry (see BOUNDED_ACTIVITY_RETRY).
        return await workflow.execute_activity(
            ACT_CALL_AGENT,
            args=[agent, skill_id, payload],
            start_to_close_timeout=SKILL_TIMEOUT,
        )

    async def _call_push(self, agent: str, skill_id: str, payload: dict) -> dict:
        """Push-notification variant of _call (#24): start the skill, release the activity slot,
        then durably wait for the agent's callback (relayed as a skill_completed signal).

        Every v1 invocation stays on the unary _call — the stubs answer instantly, so there is
        nothing to wait for. This path exists for M3's long-running tools: switching a skill to
        push mode is a one-word change (_call -> _call_push) with the same payload and result
        shape. Failure semantics differ from unary on purpose: the push sender POSTs each
        callback at most once, so a lost/failed callback re-runs the whole skill (fresh agent
        task, duplicate side effects) — bounded at PUSH_SKILL_ATTEMPTS, then the workflow fails
        with ApplicationError (a Temporal failure exception; anything else would fail only the
        workflow TASK and hot-retry forever, wedging the study).
        """
        attempted: list[str] = []
        try:
            for _ in range(PUSH_SKILL_ATTEMPTS):
                task_id = await workflow.execute_activity(
                    ACT_START_AGENT,
                    args=[agent, skill_id, payload, workflow.info().workflow_id],
                    start_to_close_timeout=PRE_READ_TIMEOUT,  # only the ACK is awaited here
                    retry_policy=BOUNDED_ACTIVITY_RETRY,  # non-idempotent: a retry can mint a dup task
                )
                attempted.append(task_id)
                try:
                    await workflow.wait_condition(
                        lambda tid=task_id: tid in self._skill_results, timeout=SKILL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    continue  # callback lost (sender never retries a POST) -> re-run the skill
                if not self._skill_results[task_id].get("__failed__"):
                    return self._skill_results[task_id]
            raise ApplicationError(
                f"push skill {skill_id!r} on agent {agent!r} did not succeed in "
                f"{PUSH_SKILL_ATTEMPTS} attempts (tasks: {attempted})",
                type="PushSkillError",
            )
        finally:
            # Reclaim every buffered outcome this call produced (late duplicates for these ids
            # are dropped by the first-write-wins handler until the pop, then re-buffered but
            # capped by PUSH_RESULT_CAP).
            for tid in attempted:
                self._skill_results.pop(tid, None)

    def _base_payload(self, ctx: dict, **derived: Any) -> dict:
        """Skill input = StudyContext + the derived slice this skill depends on."""
        return {"studyContext": ctx, **derived}

    # ---- sign-off gate (#29) -------------------------------------------------------
    async def _ack_or_timeout(self, timeout: timedelta | None) -> bool:
        """True iff the radiologist ack arrived (already, or within `timeout`; None = wait)."""
        if timeout is not None and timeout <= timedelta(0):
            return self._signoff_ack is not None
        try:
            await workflow.wait_condition(lambda: self._signoff_ack is not None, timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def _page(self, wf_id: str, reason: str, rung: Optional[dict]) -> None:
        """Fire one escalation dispatch (escalate_activity -> comms.dispatch), best-effort.

        Bounded retries cover a transient comms blip, but a persistent outage must NOT strand
        the durable gate -- we log and let the ladder keep climbing (or the gate keep holding),
        so the next rung still fires on time.
        """
        try:
            await workflow.execute_activity(
                ACT_ESCALATE,
                args=[wf_id, reason, rung],
                start_to_close_timeout=SKILL_TIMEOUT,
                retry_policy=BOUNDED_ACTIVITY_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "escalation paging failed for %s (rung level=%s); gate continues",
                wf_id, (rung or {}).get("level"),
            )

    async def _hold_signoff_gate(self, wf_id: str) -> None:
        """Hold AWAITING_SIGNOFF until the radiologist acks, escalating per the tier ladder (#29).

        The orchestrator owns the durable escalation clock (the real comms agent has no
        self-firing timer): each rung of escalation-policy.yaml fires as its afterMinutes --
        anchored to gate entry, so a slow/failed page never delays later rungs -- elapses without
        an ack, paging a widening audience. A repeating final rung keeps re-firing at its cadence
        (capped by ESCALATION_REPEAT_CAP); either way the gate only opens on the ack signal, and
        the verify loop then re-checks. If the policy itself cannot be loaded, fall back to the
        legacy pre-#29 gate (single tier timeout, one flat page, back to the verify loop).
        """
        tier = (self._triage or {}).get("priorityTier")
        reason = "sign-off gate timed out awaiting radiologist"
        try:
            ladder: list = await workflow.execute_activity(
                ACT_LOAD_ESCALATION_POLICY,
                args=[tier],
                start_to_close_timeout=PRE_READ_TIMEOUT,
                retry_policy=BOUNDED_ACTIVITY_RETRY,
            )
        except ActivityError:
            ladder = []
        if not ladder:  # unavailable or (defensively) empty -> legacy single-timeout gate
            workflow.logger.warning(
                "escalation policy unavailable for %s; using legacy single-timeout gate", wf_id
            )
            if not await self._ack_or_timeout(signoff_timeout_for(tier)):
                await self._page(wf_id, reason, None)
            return

        entered = workflow.now()
        for rung in ladder:
            target = entered + timedelta(minutes=rung["afterMinutes"])
            if await self._ack_or_timeout(target - workflow.now()):
                return
            await self._page(wf_id, reason, rung)

        last = ladder[-1]
        if last.get("repeat"):
            # Anchor the repeat cadence to gate entry too (like the ladder rungs above), so a slow
            # or failed page never pushes out later re-fires: attempt N is due at the final rung's
            # target plus (N-1) cadences, and a <=0 wait fires immediately to catch back up.
            base = entered + timedelta(minutes=last["afterMinutes"])
            cadence = timedelta(minutes=last["repeatEveryMinutes"])
            for attempt in range(2, ESCALATION_REPEAT_CAP + 1):
                if await self._ack_or_timeout(base + (attempt - 1) * cadence - workflow.now()):
                    return
                await self._page(wf_id, reason, {**last, "attempt": attempt})
            workflow.logger.warning(
                "escalation repeat cap (%d) reached for %s; gate holds without further paging",
                ESCALATION_REPEAT_CAP, wf_id,
            )
        await self._ack_or_timeout(None)

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

            # AWAITING_SIGNOFF: hold for the radiologist addendum/ack, climbing the tier's
            # escalation ladder (#29) while the report sits unsigned.
            self._state = State.AWAITING_SIGNOFF
            self._signoff_ack = None
            await self._hold_signoff_gate(wf_id)
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

    @workflow.signal
    def skill_completed(self, event: dict) -> None:
        """A push-mode skill finished (#24): ingress relays the agent's callback here.
        event = {"taskId": ..., "result": {...}} — or {"taskId": ..., "failed": true}.
        First write wins per taskId: both signals of a duplicated terminal can land in ONE
        workflow activation (i.e. before the waiter wakes), and a late synthesized failure
        must not overwrite a good result. Size-capped: orphaned taskIds (e.g. from an
        activity retry's duplicate task) are never awaited, so evict oldest-first."""
        task_id = event.get("taskId")
        if not task_id or task_id in self._skill_results:
            return
        while len(self._skill_results) >= PUSH_RESULT_CAP:
            self._skill_results.pop(next(iter(self._skill_results)))
        if event.get("failed"):
            self._skill_results[task_id] = {"__failed__": True}
        else:
            self._skill_results[task_id] = event.get("result") or {}

    @workflow.query
    def current_state(self) -> str:
        return self._state.value
