# CLAUDE.md — Communications Agent

**Owner:** Pranathi (lead) · **Skills:** `comms.dispatch` / `comms.checkAck` / `comms.escalate` · **Port:** 8106 · **Stage:** Communicate

## What this is
The existing LH Communications service, being grown into the real **CritCom** (Critical-Results
Communication) agent (#52). The orchestrator hands off at COMMUNICATE and drives the
notify → check-ack → escalate loop. Three skills:
- **`comms.dispatch`** — classify the finding (ACR), page the right provider, open an ack clock.
- **`comms.checkAck`** — poll the acknowledgement Task; reports status + `overdue`.
- **`comms.escalate`** — escalate an unacknowledged critical result to the on-call provider.

## Contract
Per-skill schemas: `contracts/skills/comms.dispatch.schema.json`,
`comms.checkAck.schema.json`, `comms.escalate.schema.json` (each: top-level = output,
`$defs/input` = input). Card: `contracts/cards/communications.json`. The orchestrator acts on
`taskId` + `deadline` + escalate's `escalated` boolean; `acrCategory` is optional/informational.

## v1 vs later
- **v1 (#52 MR 1, contracts):** `comms.dispatch` keeps the #17 channel-selection stub (routine →
  EHR inbox; critical → also page on-call; criticality = impression `criticalFlags` or a failed
  verification). A sign-off escalation rung (#29) arrives as an `escalation` input slice
  (`escalation-policy.schema.json` `$defs/dispatchEscalation`) whose channels are dispatched as
  requested — the ladder already chose who/how. `comms.checkAck` / `comms.escalate` return
  contract-valid stubs. Delivery is stubbed (no real EHR/pager/SMS I/O).
- **#52 MR 2/3:** swap the FHIR client to `radagent_common.fhir_client` (reads) + a separate
  comms-ledger (Communication/Task writes); port CritCom's tools + the ACR classifier stub, so
  `comms.dispatch` returns real `communicationId`/`taskId`/`deadline`.
- **M3:** real channel delivery + real Gemini ACR classifier.

> Note: #29's ladder is the "radiologist didn't SIGN" gate; CritCom's checkAck/escalate loop is the
> "physician didn't ACK a critical result" gate. Different gates — do not double-page.

## Run / test
`cd agents/communications && python -m pytest -q`
Run as a server: `uvicorn server:asgi_app --port 8106`

## Do NOT touch
Other agents, `orchestrator/`, the shared envelope, the A2A factory (`radagent_common/a2a.py`).
