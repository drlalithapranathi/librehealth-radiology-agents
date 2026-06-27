# CLAUDE.md — Report Verification Agent

**Engine owner:** Pranathi (lead) · **Rules owner:** Saptarshi (PI)
**Skill:** `report.verify` · **Port:** 8105 · **Stage:** Report

## You own
- **Engine (lead):** `rules/engine.py`, `handler.py`, `server.py`, `tests/`, card.
- **Rules (PI):** `rules/*.yaml` and `rules/custom/*.py`. Add rules WITHOUT touching the engine.

## Contract
Return shape: `contracts/skills/report.schema.json` (`verificationStatus` PASS|WARN|FAIL,
`requiresHumanReview`, `issues[]` with `ruleId`/`severity`/`message`/`location`).

## Authoring rules (PI)
Declarative YAML (format described below). A rule's `when` describes the **problem** condition; if it
evaluates true, an issue is emitted. Ops: exists, not_exists, empty, non_empty, equals,
not_equals, contains, gt, lt. Paths are dotted and may index lists
(`impression.structuredFindings.0.laterality`). Complex logic → `rules/custom/<id>.py` with
`def check(ctx) -> dict | None`. Context keys: `report`, `impression`, `ehrContext`, `aiFindings`.

Status: FAIL if any FAIL issue, else WARN if any WARN, else PASS. `requiresHumanReview` is true
for WARN/FAIL.

## v1 vs later
- **v1:** engine + two sample rules + one custom example.
- **M2:** PI rule library grows; report-body parsing feeds richer fields (laterality, sections).

## Run / test
`cd agents/report-verification && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
