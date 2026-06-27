# CLAUDE.md — repo guide for Claude Code

You are working in the **LibreHealth Radiology multi-agent system** monorepo. Read this
file, then the `CLAUDE.md` in the directory you're touching. Diagrams are in
**`ARCHITECTURE.md`**; the authoritative contracts are in **`/contracts`**. The **backlog
(what's left to build) lives in GitLab issues**, grouped by the `M0–M3` milestones — not in
a doc in this repo.

## Golden rules (do not break these)
1. **Contract-first.** `/contracts/*.schema.json` and `/contracts/cards/*.json` are the
   source of truth. Change a payload shape and the schema in the **same change**. CI
   (`scripts/validate_contracts.py`) enforces it.
2. **Lean-reference, never PHI in messages.** A2A messages carry IDs + correlation only.
   Fetch clinical data from `fhir2` (`radagent_common.fhir_client`) and imaging metadata from
   Orthanc (`radagent_common.orthanc_client`). Derived results flow via the orchestrator's
   `WorkflowState` (pass-forward), not by re-fetching from source.
3. **Scope discipline.** Edit only the directory you own (see Ownership below). The
   orchestrator and the shared envelope are lead-reviewed.
4. **Handlers are pure.** An agent handler is `async def handle(skill_id, payload) -> dict`.
   It imports `radagent_common` and its own siblings — **never `a2a.*`** (all A2A plumbing is
   isolated in `radagent_common/a2a.py`). This keeps handler tests fast and SDK-agnostic.
5. **Determinism in workflows.** `orchestrator/workflow.py` has no I/O, no wall-clock, no
   randomness. All side effects go in `orchestrator/activities.py`.

## House stack
Python 3.11 · FastAPI · **a2a-sdk** (official, 1.0 — pinned in `pyproject.toml`) ·
**Temporal** (`temporalio`) · pydantic v2 · jsonschema · pyyaml.

## Repo map
```
contracts/        SOURCE OF TRUTH: studycontext + per-skill schemas + events + agent cards
libs/radagent-common/   shared lib: StudyContext, A2A factory, fhir2/orthanc clients, validation
orchestrator/     Temporal workflow (state machine) + activities + ingress (Orthanc rx, RIS poller)
agents/<name>/    one A2A agent each — standalone root (hyphenated dir, NOT a package)
integrations/     orthanc-plugin · worklist-api · ohif-extension (M2)
mocks/            walking skeleton + mock agent + synthetic fixtures
scripts/          validate_contracts.py (CI gate)
```

## Glossary
- **A2A** — agent-to-agent protocol; each agent serves a card at `/.well-known/...` and typed
  JSON skills. The orchestrator is the A2A client.
- **StudyContext** — the canonical lean envelope passed to every skill (IDs + correlation, no
  PHI). Schema: `contracts/studycontext.schema.json`.
- **DICOM MWL** — acquisition-side modality worklist (scanners). **Not** what we build.
- **Reading worklist** — the radiologist's list of studies to read; served by the **Worklist API**.
- **RIS** — the LH-Radiology reporting UI where the radiologist authors & signs.
- **fhir2** — OpenMRS FHIR R4 module; our EHR data API, used read-mostly.
- **Human-gated state** — a workflow state that blocks on a radiologist action (read, sign-off);
  Temporal durable timers + signals handle the wait.

## Ownership
| Owner | Workstream |
|-------|------------|
| **Pranathi** (lead) | contracts + shared libs; orchestrator + Temporal workflows; ingress (Orthanc rx + RIS poller); Report Verification engine; mock harness |
| **Parvati** (senior) | Worklist Triage; EHR Assistant; Orthanc plugin; Worklist API; (M2) OHIF data source |
| **Chaitra** (junior) | Impression Generation; Interpretation Assistant (registry + stubs); mocks & fixtures |
| **Saptarshi** (PI) | Verification rules/config (YAML); cross-MR review; contract & schema sign-off |

## Locked decisions (do not relitigate without lead sign-off)
- 5 agents in scope; the Communications Agent already exists (A2A + FastAPI) and is conformed
  to the `comms.dispatch` contract.
- Report **authored & signed in LH-Radiology RIS**; sign-off detected via fhir2 polling
  `DiagnosticReport?status=final&_lastUpdated=gt{cursor}` (M2: Atomfeed real-time upgrade).
- **Temporal** is the orchestration engine; one workflow instance per study.
- **AI models stubbed** in v1 behind the Interpretation tool registry (real tools M3).
- **Lean-reference + pass-forward** payloads (see Golden rule 2).
- **Radiologist assignment is owned by LH-Radiology** (specialty + case importance + call
  times); the Worklist API reads it read-only and never writes it.
- Worklist priority source of truth = orchestrator state; **no DICOM tag mutation**.
- v1: Impression + Verification run **post-sign** as a read-only safety-net; contracts are
  timing-agnostic so M2 can turn on pre-sign assist without changing them.

## Run / verify
```bash
pip install -e libs/radagent-common pytest pytest-asyncio   # one-time
python scripts/validate_contracts.py                        # contracts hold together?
python mocks/run_walking_skeleton.py                        # whole pipeline in-process, validated
cd agents/worklist-triage && python -m pytest -q            # an agent's tests (run from its dir)
cd agents/worklist-triage && uvicorn server:asgi_app --port 8101   # run an agent as A2A server
python -m orchestrator.worker                               # orchestrator (needs Temporal up)
uvicorn orchestrator.ingress:app --port 8090
```

## Status
M0 (contract freeze + harness). Agents return validated stubs; real AI, live fhir2/Orthanc
reads, A2A transport, and the Temporal end-to-end run land in M1. Search `TODO(M1)` /
`TODO(M2)` for next steps; the full plan is the GitLab issue backlog.
