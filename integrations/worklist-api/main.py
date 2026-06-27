"""Worklist API — serves OHIF a sorted *reading* worklist (distinct from DICOM MWL).

Joins: Orthanc completed studies + orchestrator priority (source of truth) +
LH-Radiology radiologist assignment (assignment is OWNED BY LH-Radiology:
specialty + case importance + call times — read-only here, never written).

Owner: Parvati. Stubs for M0; wire data sources in M2. No DICOM tag mutation.
"""
from __future__ import annotations
from fastapi import FastAPI

app = FastAPI(title="LH-Radiology Worklist API")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/worklist")
async def worklist() -> dict:
    """Return studies sorted by orchestrator priority, annotated with assignment.

    TODO(M2):
      1. orthanc = OrthancClient(); studies = await orthanc.list_completed_studies()
      2. priorities = <read orchestrator priority store / WorkflowState query>
      3. assignment = <read from LH-Radiology RIS (read-only)>
      4. join + sort by priorityTier/priorityScore, return for the OHIF data source.
    """
    return {"items": [], "note": "stub — see TODO(M2)"}
