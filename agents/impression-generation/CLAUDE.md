# CLAUDE.md — Impression Generation Agent

**Owner:** Chaitra · **Skill:** `impression.generate` · **Port:** 8104 · **Stage:** Report

## You own
`handler.py`, `server.py`, `tests/`, card `contracts/cards/impression-generation.json`.

## Contract
Return shape: `contracts/skills/impression.schema.json` (`impressionText`,
`structuredFindings[]`, `recommendations[]`, `criticalFlags[]`).

## Timing (IMPORTANT)
The skill is **timing-agnostic**. v1 runs **post-sign** as a safety-net / structuring step;
M2 turns on **pre-sign assist** with the same I/O. Don't bake a timing assumption into the
output — just consume the inputs you're given.

## v1 vs later
- **v1:** deterministic stub draft.
- **M2:** LLM draft from `report` + `ehrContext` + `aiFindings` + priors.
- Populate `criticalFlags` when you detect a critical finding — Report Verification keys on it.

## Data deps
Inputs are passed in (`report`, `ehrContext`, `aiFindings`); fetch priors from `fhir2` if needed.

## Run / test
`cd agents/impression-generation && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
