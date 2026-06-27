# CLAUDE.md — Interpretation Assistant Agent

**Owner:** Chaitra · **Skill:** `interpretation.runTools` · **Port:** 8103 · **Stage:** Interpretation

## You own
`handler.py`, `registry.py` (tool selection), `server.py`, `tests/`,
card `contracts/cards/interpretation-assistant.json`.

## Contract
Return shape: `contracts/skills/interpretation.schema.json` (`toolsSelected[]`, `findings[]`
with `evidenceRef`, `overallStatus` STUBBED|COMPLETE|PARTIAL|ERROR).

## v1 vs later
- **v1:** `registry.select_tools(modality, description)` picks tool names; results are
  `STUBBED`. The registry interface is the contract — keep it stable.
- **M3:** wire real CAD/detection tools behind the same registry; emit `evidenceRef` to
  DICOM SC/overlay objects.

## Data deps
Orthanc imaging metadata via `radagent_common.orthanc_client` (M3). No PHI in messages.

## Run / test
`cd agents/interpretation-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
