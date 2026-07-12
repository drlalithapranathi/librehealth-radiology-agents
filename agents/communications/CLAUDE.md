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

## The two stores
Clinical context is READ from **fhir2** (`radagent_common.fhir_client`, read-only). The
notification and its ack are WRITTEN to the **comms ledger** (`radagent_common.comms_ledger`,
a separate HAPI FHIR server) — fhir2 implements neither `Communication` nor `PractitionerRole`.
See `comms_ledger.py` for the full reasoning.

`resolve_ordering_provider` carries `ServiceRequest.requester` **verbatim** and never dereferences
it: the order lives in fhir2 and the on-call directory in the ledger, so following that reference
across stores would be a guess about matching ids. Recording who we notified does not require
dialling them. The on-call provider IS dereferenced — `PractitionerRole` is ledger-native.

## The closed loop (#52 MR 3, real)
`comms.dispatch` classifies (ACR), and for a **critical** result writes a `Communication` ("we told
someone") + an ack `Task` ("did they answer"), returning `communicationId` / `taskId` / `deadline` /
`recipient`. A **routine** result posts to the EHR inbox and opens **no ack clock** — a timer on a
clean chest X-ray is how alert fatigue starts. `comms.checkAck` reads the Task; `comms.escalate`
marks it FAILED and opens a fresh loop on the on-call provider.

Ack windows: Cat1 = 60 min, Cat2 = 24 h (`CRITCOM_CAT{1,2}_ACK_TIMEOUT_MINUTES`).

**The agent never self-fires a timer.** It opens the clock and reports the deadline; the
orchestrator owns the durable wait and calls back (#52 MR 4).

## v1 vs later
- **v1:** the ACR classifier is deterministic — it reads Impression's `criticalFlags` and
  Verification's status, not the narrative (`classifier.py`). Channel delivery is still stubbed
  (no real EHR/pager/SMS I/O); what is real is the FHIR record of it.
- **M3:** real channel delivery + the real Gemini ACR classifier behind the same `classify()`
  signature (the pattern `interpretation-assistant/registry.py` uses).

> ## Two gates. Do not double-page.
> **#29's ladder** = "the radiologist didn't SIGN". Its fired rung arrives as an `escalation` input
> slice on `comms.dispatch`; the ladder already chose who/how, so those channels are dispatched
> verbatim and **no ack clock is opened** — there is no signed report to acknowledge.
> **checkAck/escalate** = "the physician didn't ACK a critical result", on a report that IS signed.

## Run / test
`cd agents/communications && python -m pytest -q`
Run as a server: `uvicorn server:asgi_app --port 8106`

## Do NOT touch
Other agents, `orchestrator/`, the shared envelope, the A2A factory (`radagent_common/a2a.py`).
