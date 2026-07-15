# CLAUDE.md — Interpretation Assistant Agent

**Owner:** Chaitra · **Skill:** `interpretation.runTools` · **Port:** 8103 · **Stage:** Interpretation

## You own
`handler.py`, `registry.py` (tool selection), `server.py`, `tests/`,
card `contracts/cards/interpretation-assistant.json`.

## Contract
Return shape: `contracts/skills/interpretation.schema.json` (`toolsSelected[]`, `findings[]`
with `evidenceRef`, `overallStatus` STUBBED|COMPLETE|PARTIAL|ERROR).

## Three levels of reality — and the output says which
`registry.select_tools(modality, description)` picks the tools; each then reports at one of three
levels, and `toolsSelected[].version` names it rather than leaving you to guess.

- **PIXELS — `cxr-screen` (#27).** The one tool that actually looks at the image. Pulls the study's
  first instance from Orthanc (`radagent_common.orthanc_client`), decodes it
  (`radagent_common.imaging`), and runs a pretrained TorchXRayVision DenseNet-121 (`cxr_model.py`).
  Reports `COMPLETE` with a real confidence and `evidenceRef: orthanc:instance/<id>`.
  Version reads `cxr-densenet121-res224-all`.
- **REFERRAL REASON — `pneumothorax-detect`, `pe-detect` (#27).** Cross-check `order.reasonCode`,
  not pixels. Matched by ICD-10 family **prefix** (dot-stripped, e.g. `I26`, `J93`), the same
  normalisation `worklist-triage` uses on the same field — an exact-code list silently disagreed
  with triage on bare `I26` / `I2699`. A genuine but narrow interim signal, not CAD, so they stay
  `STUBBED` with a plain-text `evidenceRef` (`order.reasonCode=J93.1`). Table-driven via
  `_REASON_CODE_RULES`. Version reads `referral-rule-1`.
- **STUBBED — everything else,** until it gets a real implementation. Version reads `stub-0`.

## The rules a real model does not get to break
- **Never invent a negative.** No extras / no instances / no pixels → `STUBBED`. Model throws →
  `ERROR`. A tool that could not look must not report "nothing found" — that is the automation-bias
  trap the #26 `COMPLETE`-gate exists to prevent, and it is *worse* from a model than from a stub,
  because it carries a model's authority. A tool that did not run must not claim a version either.
- **The model scores anything.** Handed uniform noise it returns `Lung Opacity: 0.996` — measured.
  There is no in-distribution check, so **the registry's modality+region selection is the only thing
  keeping a non-CXR away from it.** Widen `chest` carelessly and you are feeding films to a model
  that will confidently rate them.
- **`COMPLETE` does not authorise a chart write.** It used to imply one: #26 gates the pre-sign
  fhir2 draft on "at least one `COMPLETE` finding", which stayed inert only while every tool was a
  stub. `PRESIGN_WRITE_ENABLED` (orchestrator `activities.py`) now separates the two decisions.
- **Pixels are PHI.** Fetched, scored, discarded. Only the derived finding travels (golden rule 2).

## Deps
The pixel path needs `radagent-common[imaging]` + torch; the Dockerfile installs both and **bakes the
model weights at build time**. `handler.PIXEL_TOOLING` records at import whether they are present —
the `agent-tests` CI lane installs neither, so `cxr-screen` degrades to `STUBBED` there and the suite
stays torch-free. The pixel path is still tested in that lane, through a seam (`tests/test_cxr_screen.py`),
and `cxr_model.py` itself runs under a torch-gated test (`tests/test_cxr_model.py`, skipped without torch).

- **DICOM SC/overlay evidenceRef is still deferred.** *Reading* pixels is now supported; **writing**
  AI-made images/overlays back into the patient record is not, and needs a safety review that hasn't
  happened (#59, blocked on #30). `evidenceRef` stays a text locator.

## Data deps
Orthanc via `radagent_common.orthanc_client`: metadata AND (since #27) pixels — `list_study_instances`
+ `get_instance_dicom`. Pixels are PHI: fetched, scored, discarded. No PHI in messages.

## Run / test
`cd agents/interpretation-assistant && python -m pytest -q`

## Do NOT touch
Other agents, `orchestrator/`, shared envelope, the A2A factory.
