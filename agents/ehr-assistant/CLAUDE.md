# CLAUDE.md — EHR Assistant Agent

**Owner:** Parvati · **Skill:** `ehr.assembleContext` · **Port:** 8102 · **Stage:** Interpretation

## You own
`handler.py`, `server.py`, `tests/`, card `contracts/cards/ehr-assistant.json`.

## Contract
Return shape: `contracts/skills/ehr.schema.json` — a **distilled** packet (priorStudies,
relevantLabs, activeProblems, contrastFlags, medicationFlags, allergies) as references +
minimal derived values. **Not** a raw record dump (lean-reference / PHI minimization).

## v1 vs later
- **v1:** returns an empty-but-valid packet.
- **M1:** fetch from `fhir2` via `radagent_common.fhir_client.Fhir2Client` (READ-ONLY).
  Use `mocks/fixtures/fhir_bundle.sample.json` to develop offline.

## Data deps
`fhir2` reads only. Never write. Never put full clinical text in the response — surface the
decision-relevant slice + references.

## Run / test
`cd agents/ehr-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
