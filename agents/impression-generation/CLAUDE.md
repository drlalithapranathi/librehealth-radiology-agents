# CLAUDE.md — Impression Generation Agent

**Owner:** Chaitra · **Skill:** `impression.generate` · **Port:** 8104 · **Stage:** Report

## You own
`handler.py`, `llm_draft.py`, `server.py`, `tests/`, card `contracts/cards/impression-generation.json`.

## Contract
Return shape: `contracts/skills/impression.schema.json` (`impressionText`,
`structuredFindings[]`, `recommendations[]`, `criticalFlags[]`).

## Timing (IMPORTANT)
The skill is **timing-agnostic**. v1 runs **post-sign** as a safety-net / structuring step;
M2 turns on **pre-sign assist** with the same I/O. Don't bake a timing assumption into the
output — just consume the inputs you're given.

## v1 vs later
- **v1:** deterministic stub draft.
- **#77 (config-gated):** `impressionText`/`recommendations` prose is LLM-authored (`llm_draft.py`)
  when `IMPRESSION_LLM_BASE_URL`/`IMPRESSION_LLM_MODEL` are set — a hosting-agnostic
  OpenAI-chat-completions HTTP shape, so the hosting choice (local open-weights vs. a
  DUA-compliant cloud service) is a config value, not a code branch. Unset (the default) or ANY
  failure/timeout/misconfiguration falls back to the deterministic template.
- `criticalFlags` (and `structuredFindings`) stay a deterministic derivation always — never
  influenced by the LLM path; see #78. Report Verification keys on `criticalFlags`.

## Data deps
Inputs are passed in (`report`, `ehrContext`, `aiFindings`); fetch priors from `fhir2` if needed.

## Run / test
`cd agents/impression-generation && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
