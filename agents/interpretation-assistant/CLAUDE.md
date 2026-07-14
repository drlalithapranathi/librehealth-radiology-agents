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
- **Real slices so far (#27):** `pneumothorax-detect` and `pe-detect` each cross-check the
  referral reason code (order.reasonCode) rather than reading pixels. Both run through the
  same table-driven `_reason_finding` rule (`_REASON_CODE_RULES` in `handler.py`) instead of
  one hand-copied function per tool. `evidenceRef` is plain text (e.g.
  `"order.reasonCode=J93.1"`), not an image ref. Every other tool stays `STUBBED` until it
  gets its own real implementation.
- **DICOM SC/overlay evidenceRef is deferred, not v1/M3 scope here.** Writing AI-made
  images/overlays into the patient record needs a safety review that hasn't happened yet, and
  it's a shared-lib (`orthanc_client.py`) change — **do not touch that file from this agent.**
  That work is tracked separately (#59) and owned outside this directory.

## Data deps
Orthanc imaging metadata via `radagent_common.orthanc_client` (M3). No PHI in messages.

## Run / test
`cd agents/interpretation-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
