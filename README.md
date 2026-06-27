# LH-Radiology Multi-Agent System

Five A2A agents + a Temporal orchestrator that drive a radiology study from PACS arrival
through result communication, on **LibreHealth Radiology** (OpenMRS) as EHR/RIS, **fhir2** as
the FHIR R4 data API, **Orthanc** PACS, and **OHIF** viewer.

- **Backlog (what is left to build):** GitLab issues, grouped by the `M0–M3` milestones
- **Architecture & decisions:** [`CLAUDE.md`](./CLAUDE.md) + [`ARCHITECTURE.md`](./ARCHITECTURE.md); contracts in [`contracts/`](./contracts)
- **Diagrams:** [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- **Working in here with Claude Code:** [`CLAUDE.md`](./CLAUDE.md)

This commit is **M0** — contract freeze + a runnable harness. Agents return validated stubs;
real AI, live fhir2/Orthanc reads, A2A transport, and the Temporal end-to-end run land in M1.

## Quickstart

```bash
# 1. Install the shared library + test deps (one venv for the monorepo)
python -m venv .venv && . .venv/bin/activate
pip install -e libs/radagent-common pytest pytest-asyncio

# 2. Verify the contracts hold together (CI runs this too)
python scripts/validate_contracts.py

# 3. Run the whole pipeline in-process — no Temporal / no servers needed.
#    Exercises all five handlers in workflow order and validates every hop.
python mocks/run_walking_skeleton.py

# 4. Run an agent's tests (agents are standalone roots — run from inside the dir)
cd agents/worklist-triage && python -m pytest -q
```

To run the full dev stack (Orthanc, OHIF, OpenMRS, Temporal), see `docker-compose.yml`.
For the live A2A + Temporal wiring, install the app extras and pin the SDKs:
`pip install -e . ` (see `pyproject.toml`; **pin `a2a-sdk` and `temporalio`**).

## Layout
| Path | What |
|------|------|
| `contracts/` | Source of truth: StudyContext, per-skill schemas, events, agent cards |
| `libs/radagent-common/` | Shared: StudyContext model, **A2A factory**, fhir2/Orthanc clients, validation |
| `orchestrator/` | Temporal workflow (state machine), activities, ingress (Orthanc rx + RIS poller) |
| `agents/<name>/` | One A2A agent each (standalone root) |
| `integrations/` | Orthanc plugin · Worklist API · OHIF extension (M2) |
| `mocks/` | Walking skeleton, mock agent, synthetic fixtures |

## Ownership
Pranathi (contracts + orchestrator + verification engine + ingress) · Parvati (triage + EHR +
Worklist API + Orthanc plugin) · Chaitra (interpretation + impression + mocks) · Saptarshi
(verification rules + review). Details: see `CLAUDE.md` (Ownership) and the GitLab backlog.
