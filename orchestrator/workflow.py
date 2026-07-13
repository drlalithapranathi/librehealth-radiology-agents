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
from datetime import datetime, timedelta, timezone
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
    ACT_WRITE_PRESIGN_IMPRESSION,
    ACT_RECORD_POLICY_FAILURE,
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
# Temporal patch marker for the pre-sign impression block (#26). A workflow's code must replay its
# own history deterministically, so inserting activity calls mid-path is a breaking change for
# every study already past that point. See the call site in run().
PATCH_PRESIGN_IMPRESSION = "presign-impression-v1"
# Temporal patch marker for the escalation-policy dead-letter write (#54). Same hazard as the
# presign marker: this inserts a new activity command into the sign-off-gate fallback branch, a
# path that studies parked at the gate have ALREADY walked. Without the guard, replaying such a
# study's history against this code finds a command that was not there when it ran and fails with
# NondeterminismError -- wedged mid-gate. Worse, the fallback fires precisely when the policy is
# broken, so deploying this fix could wedge many parked studies at once. patched() makes an OLD
# history skip the write (it never happened for that study) while every NEW study records it.
# Retire the marker (-> workflow.deprecate_patch) only once no pre-#54 workflow is open.
PATCH_POLICY_DEAD_LETTER = "policy-dead-letter-v1"
# The critical-results ack loop (#52). CritCom OPENS the ack clock and reports its deadline; it has
# no self-firing timer, so if the orchestrator does not watch the clock, nobody does -- a Cat1
# finding gets a 60-minute deadline that no one is waiting on. The orchestrator owns the durable
# wait (Temporal timers survive restarts), re-checks the Task at the deadline, and escalates an
# unacknowledged critical result to on-call.
#
# Bounded, deliberately. Each escalation opens a FRESH loop on a new person, so an uncapped chase
# would page forever and grow history without bound; after the cap the study archives with
# ackStatus recorded rather than hanging (see the sign-off gate, which does hang -- that is a known
# hole, not a pattern to copy). ACK_GRACE absorbs clock skew between us and the ledger: without it,
# waking exactly ON the deadline can find the Task not-yet-overdue and spin.
#
# Guarded by a patch marker like the other two. The loop appends its commands at the very end of the
# path, so an OPEN study replays fine without it -- but a CLOSED one does not, and closed histories
# ARE replayed: a query against a finished study (and Temporal's own reset) replays its history, and
# there the recorded next event after comms.dispatch is WorkflowExecutionCompleted while this code
# now schedules comms.checkAck. That is a command/event mismatch on every study we have already
# archived. Retire the marker (-> workflow.deprecate_patch) once no pre-#52 history is queried.
PATCH_ACK_LOOP = "critcom-ack-loop-v1"
ACK_ESCALATION_CAP = 3
ACK_GRACE = timedelta(minutes=1)
# Total loop iterations, counting re-checks that found the Task not yet overdue. A backstop against
# a ledger whose clock is far behind ours: without it, "not overdue -> wait again" could spin.
ACK_LOOP_CAP = 2 * ACK_ESCALATION_CAP + 2
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

    # ---- pre-sign impression assist (#26) -------------------------------------------
    def _has_complete_finding(self) -> bool:
        """Did any Interpretation tool actually produce a real result for this study?

        The v1 registry returns every finding as STUBBED with an empty label -- no tool has run.
        The impression then has nothing to work from and falls through to its constant fallback
        ("No acute findings identified. Clinical correlation recommended."), so writing that draft
        would put a fixed NEGATIVE impression, authored by nobody, into every patient's chart ahead
        of the read. That is the automation bias the post-sign rule existed to prevent.

        So the write is gated on the pre-sign draft actually knowing something: at least one
        COMPLETE finding. Inert until the real tools land in M3, live automatically after.
        A hard condition of the amended locked decision (CLAUDE.md, #26).
        """
        findings = (self._ai or {}).get("findings") or []
        return any(f.get("status") == "COMPLETE" for f in findings)

    async def _presign_impression(self, ctx: dict) -> None:
        """Offer an aiFindings-only draft into the RIS while the radiologist is still reading.

        Both calls are advisory: bounded retries (BOUNDED_ACTIVITY_RETRY), then skip on failure,
        so a down impression-generation agent or a failed fhir2 write never strands the human
        read that follows -- the v1 post-sign safety-net covers the study regardless.
        """
        if not self._has_complete_finding():
            workflow.logger.info(
                "no COMPLETE aiFinding for %s; skipping the pre-sign draft (nothing to offer)",
                ctx["workflowId"],
            )
            return

        try:
            impression = await workflow.execute_activity(
                ACT_CALL_AGENT,
                args=["impression-generation", "impression.generate",
                      self._base_payload(ctx, ehrContext=self._ehr, aiFindings=self._ai)],
                start_to_close_timeout=SKILL_TIMEOUT,
                retry_policy=BOUNDED_ACTIVITY_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "pre-sign impression.generate failed for %s; skipping draft", ctx["workflowId"]
            )
            return

        service_request_ref = (ctx.get("order") or {}).get("fhirServiceRequestId")
        if not service_request_ref:
            return  # nowhere in the RIS to attach the draft yet

        try:
            await workflow.execute_activity(
                ACT_WRITE_PRESIGN_IMPRESSION,
                args=[service_request_ref, ctx["patient"]["fhirPatientId"], impression["impressionText"]],
                start_to_close_timeout=SKILL_TIMEOUT,
                retry_policy=BOUNDED_ACTIVITY_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "pre-sign RIS write failed for %s; draft not offered", ctx["workflowId"]
            )

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

    async def _record_policy_failure(self, wf_id: str, tier: str | None) -> None:
        """Make a collapsed escalation ladder operator-visible (#54) -- best-effort, never fatal.

        The soft fallback below is deliberate (a broken policy must degrade, not wedge the gate),
        but degraded-AND-silent is the real hazard: the ladder shrinks to one flat page and nothing
        says so. This records a dead letter next to the poller's on /admin/dead-letters.

        Swallowed on failure on purpose: the alert is observability, and the study still has to be
        escalated and read. If we cannot even write the alert, the warning log above stands and the
        gate proceeds -- an unwritable store must never cost a radiologist their page.
        """
        try:
            await workflow.execute_activity(
                ACT_RECORD_POLICY_FAILURE,
                args=[wf_id, tier, "escalation policy could not be loaded",
                      BOUNDED_ACTIVITY_RETRY.maximum_attempts],
                start_to_close_timeout=PRE_READ_TIMEOUT,
                retry_policy=BOUNDED_ACTIVITY_RETRY,
            )
        except ActivityError:
            workflow.logger.warning(
                "could not record the escalation-policy dead letter for %s; "
                "the gate still falls back and pages", wf_id
            )

    # ---- the critical-results ack loop (#52) -----------------------------------------
    @staticmethod
    def _deadline(iso: str) -> Optional[datetime]:
        """Parse an agent-reported ISO deadline. Pure -- no I/O, no wall-clock read -- so it is safe
        inside the workflow sandbox (golden rule 5). A naive timestamp is read as UTC rather than
        raising when compared against workflow.now(), which is always tz-aware."""
        try:
            dt = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    async def _await_ack(self, ctx: dict, comms: dict) -> dict:
        """Watch the ack clock CritCom opened, and escalate a critical result nobody acknowledges.

        The agent records that we told someone and by when they must answer -- and then stops. It
        has NO self-firing timer, so without this loop a Cat1 finding carries a 60-minute deadline
        that nothing is waiting on: the Task sits `requested` in the ledger forever and the on-call
        provider is never told. The orchestrator owns the durable wait; Temporal timers survive a
        worker restart, which is the whole reason the clock lives here and not in the agent.

        Always returns, and the study always archives. An unacknowledged result is recorded as such
        (`ackStatus: UNACKNOWLEDGED`) rather than hanging the workflow -- a stranded study helps
        nobody, and the escalation itself has already paged a human.
        """
        task_id = comms.get("taskId")
        deadline = self._deadline(comms.get("deadline") or "")
        if not task_id or deadline is None:
            # No clock was opened: a routine result, a SKIPPED dispatch, or a sign-off-ladder rung
            # (#29's gate, which deliberately opens no ack loop -- see the agent's two-gates note).
            return {}

        escalations = 0
        for _ in range(ACK_LOOP_CAP):
            wait = deadline + ACK_GRACE - workflow.now()
            if wait > timedelta(0):
                await workflow.sleep(wait)

            state = await self._call(
                "communications", "comms.checkAck", self._base_payload(ctx, taskId=task_id))
            if state.get("ackStatus") == "COMPLETED":
                return {"ackStatus": "COMPLETED", "taskId": task_id, "escalations": escalations}

            if not state.get("overdue"):
                # Not actually late: the ledger's clock trails ours, or the deadline moved. Re-read
                # it and wait again -- escalating a result that is not overdue pages a human early
                # and, worse, teaches them the pages are noise.
                moved = self._deadline(state.get("deadline") or "")
                deadline = moved if moved is not None else deadline + ACK_GRACE
                continue

            if escalations >= ACK_ESCALATION_CAP:
                break

            esc = await self._call(
                "communications", "comms.escalate", self._base_payload(ctx, taskId=task_id))
            escalations += 1
            if not esc.get("escalated"):
                # Nobody left to tell -- an empty on-call directory. The agent said so honestly
                # instead of reporting a page it never sent; do not spin on it.
                workflow.logger.error(
                    "critical result on %s is unacknowledged AND unescalatable (%s); archiving",
                    ctx["workflowId"], esc.get("reason") or "no on-call provider",
                )
                return {"ackStatus": "UNACKNOWLEDGED", "taskId": task_id,
                        "escalations": escalations,
                        "reason": esc.get("reason") or "nobody to escalate to"}

            new_task = esc.get("newTaskId")
            new_deadline = self._deadline(esc.get("newDeadline") or "")
            if not new_task or new_deadline is None:
                break  # escalated, but no new loop to watch: nothing further we can do here
            task_id, deadline = new_task, new_deadline

        workflow.logger.error(
            "critical result on %s never acknowledged after %d escalation(s); archiving unacked",
            ctx["workflowId"], escalations,
        )
        return {"ackStatus": "UNACKNOWLEDGED", "taskId": task_id, "escalations": escalations}

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
            # Guarded so a study parked at the gate before this change replays deterministically
            # (see PATCH_POLICY_DEAD_LETTER). The write is best-effort either way; the guard is
            # about replay safety, not the write's own failure handling.
            if workflow.patched(PATCH_POLICY_DEAD_LETTER):
                await self._record_policy_failure(wf_id, tier)
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

        # --- PRESIGN IMPRESSION (#26): offer an aiFindings-only draft ahead of the read -----
        # Guarded by workflow.patched(): this block inserts activity commands into the middle of a
        # path that in-flight studies have ALREADY walked. A study parked at the sign-off gate when
        # the worker redeploys replays its history against the new code, finds commands that were
        # not there when it ran, and fails with NondeterminismError -- wedged, mid-gate, on a study
        # awaiting a radiologist. patched() makes the replay of an OLD history skip the block (it
        # never happened for that study) while every NEW study takes it.
        # Retire the marker (-> workflow.deprecate_patch) only once no pre-#26 workflow is open.
        if workflow.patched(PATCH_PRESIGN_IMPRESSION):
            await self._presign_impression(ctx)

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

        # The agent opened an ack clock and reported its deadline; nobody else is watching it.
        # Guarded: see PATCH_ACK_LOOP (a query against an already-archived study replays its
        # history, where the next event after the dispatch is the workflow completing).
        ack: dict = {}
        if workflow.patched(PATCH_ACK_LOOP):
            ack = await self._await_ack(ctx, comms)

        self._state = State.ARCHIVED
        return {
            "workflowId": wf_id,
            "finalState": self._state.value,
            "triage": self._triage,
            "verification": self._verification,
            "comms": comms,
            "ack": ack,
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
