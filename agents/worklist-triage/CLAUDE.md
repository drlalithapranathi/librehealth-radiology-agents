# CLAUDE.md — Worklist Triage Agent

**Owner:** Parvati · **Skill:** `triage.score` · **Port:** 8101 · **Stage:** Study Routing

## You own
`handler.py` (scoring logic), `server.py`, `tests/`, and the card `contracts/cards/worklist-triage.json`.

## Contract
Return shape: `contracts/skills/triage.schema.json` (`priorityScore` 0–100, `priorityTier`
STAT|URGENT|ROUTINE, `rationale[]`). Input: `{ studyContext }`.

## v1 vs later
- **v1:** transparent rule-of-thumb from order priority + reason codes + modality.
- **M2:** real signals (history, acuity models). Keep `rationale[]` populated — it's how
  radiologists trust the ordering.

## Data deps
None required in v1. Priority you return is published by the orchestrator to the Worklist API
(orchestrator is the source of truth — **no DICOM tag mutation**).

## Run / test
`cd agents/worklist-triage && python -m pytest -q` · serve: `uvicorn server:asgi_app --port 8101`

## Do NOT touch
Other agents, `orchestrator/`, `studycontext.schema.json`, the A2A factory.
