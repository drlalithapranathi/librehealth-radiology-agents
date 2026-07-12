# CLAUDE.md — Communications Agent

**Owner:** Pranathi (lead) · **Skill:** `comms.dispatch` · **Port:** 8106 · **Stage:** Communicate

## What this is
The existing LH Communications service, conformed to A2A (#17). The orchestrator hands off at the
COMMUNICATE state; this agent dispatches result notifications and reports per-channel status.

## Contract
Return shape: `contracts/skills/comms.schema.json` (`dispatchStatus` SENT|QUEUED|FAILED|PARTIAL,
optional `channelResults[]` of `{channel, status}`). Card: `contracts/cards/communications.json`.

## v1 vs later
- **v1:** channel *selection* by urgency (routine → EHR inbox; critical → also page on-call);
  delivery is stubbed (no real EHR/pager/SMS I/O). Criticality = impression `criticalFlags` or a
  failed verification. A sign-off escalation rung (#29) arrives as an `escalation` input slice
  (`escalation-policy.schema.json` `$defs/dispatchEscalation`) whose channels are dispatched
  as requested — the ladder already chose who/how.
- **M3:** real channel delivery + closed-loop acknowledgement.

## Run / test
`cd agents/communications && python -m pytest -q`
Run as a server: `uvicorn server:asgi_app --port 8106`

## Do NOT touch
Other agents, `orchestrator/`, the shared envelope, the A2A factory (`radagent_common/a2a.py`).
