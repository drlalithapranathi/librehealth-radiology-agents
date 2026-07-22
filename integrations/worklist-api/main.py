"""Worklist API — serves OHIF a sorted *reading* worklist (distinct from DICOM MWL).

Join sources:
  * Orthanc (source of truth for what studies exist and their DICOM metadata) via
    `radagent_common.orthanc_client.OrthancClient.list_completed_studies`;
  * orchestrator triage priority (source of truth for reading order) via a local
    durable SQLite store — orchestrator's `publish_priority_activity` POSTs to
    `/priority` when each study's triage completes;
  * LH-Radiology radiologist assignment — read-only per CLAUDE.md locked decision;
    `NullAssignmentReader` returns None for every study until the RIS integration
    is wired (M3).

Sort key: priorityTier bucket (STAT > URGENT > ROUTINE), then priorityScore
descending, then studyDate ascending (older stat cases float above newer ones).
Studies Orthanc knows about but the orchestrator has not yet triaged appear as
ROUTINE / 50 so they land at the bottom rather than being invisible.

Response shape (documented, not schema-locked — the OHIF extension is issue #21):
  {
    "items": [
      {
        "orthancStudyId":    str,
        "studyInstanceUID":  str,
        "accessionNumber":   str,
        "modality":          str,
        "studyDescription":  str,
        "studyDate":         str,   // "YYYYMMDD" from DICOM
        "priorityTier":      "STAT" | "URGENT" | "ROUTINE",
        "priorityScore":     int,   // 0..100
        "workflowId":        str | null,   // null when un-triaged
        "assignment":        {"radiologistId": str, "assignedAt": iso8601} | null
      },
      ...
    ],
    "generatedAt": iso8601
  }

Owner: Parvati. No DICOM tag mutation.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from radagent_common.comms_ledger import CommsLedgerClient
from radagent_common.orthanc_client import OrthancClient
from radagent_common.tracing import now_iso

from ack import OpenmrsIdentity, create_ack_router
from findings import create_findings_router
from findings_store import FindingsStore
from assignment import AssignmentReader, NullAssignmentReader
from store import PriorityStore

# --- Sort logic --------------------------------------------------------------
# priorityTier is the primary bucket; priorityScore ties (higher = read first);
# studyDate breaks further ties (older = read first, so a stat case queued
# yesterday floats above a stat case queued this morning).
_TIER_RANK = {"STAT": 0, "URGENT": 1, "ROUTINE": 2}
_DEFAULT_TIER = "ROUTINE"
_DEFAULT_SCORE = 50


class PriorityPush(BaseModel):
    """Body of `POST /priority` — the orchestrator's triage-published event.

    Kept flat (no nested StudyContext) because the orchestrator is the caller
    and already has all fields resolved; this endpoint is not part of the
    externally-consumed /worklist contract."""
    studyInstanceUID: str = Field(..., min_length=1)
    workflowId:       str = Field(..., min_length=1)
    priorityTier:     str = Field(..., pattern=r"^(STAT|URGENT|ROUTINE)$")
    priorityScore:    int = Field(..., ge=0, le=100)


# --- App factory -------------------------------------------------------------
# Split so tests can inject fakes without touching env vars.

def create_app(
    orthanc: Optional[OrthancClient] = None,
    store: Optional[PriorityStore] = None,
    assignment: Optional[AssignmentReader] = None,
    ledger: Optional[CommsLedgerClient] = None,
    identity: Optional[OpenmrsIdentity] = None,
) -> FastAPI:
    app = FastAPI(title="LH-Radiology Worklist API")

    app.state.orthanc = orthanc or OrthancClient()
    app.state.store = store or PriorityStore(
        os.environ.get("WORKLIST_STORE_PATH", "/var/lib/lhrad/worklist.sqlite"))
    app.state.assignment = assignment or NullAssignmentReader()

    # The #79 ack surface (see ack.py). Inert until CRITCOM_ACK_HMAC_SECRET is configured:
    # unsigned links are never minted producer-side and the verifier here fails closed.
    app.include_router(create_ack_router(
        ledger or CommsLedgerClient(), identity or OpenmrsIdentity()))

    # The #89 findings surface (see findings.py). Orchestrator publishes interpretation.runTools
    # output here after the pre-read fan-out; the OHIF extension reads back for client-side
    # AI-evidence rendering. Separate DB file from PriorityStore so the two lifecycles are
    # independent (e.g., wiping findings during dev doesn't drop worklist priority).
    app.state.findings_store = FindingsStore(
        os.environ.get("WORKLIST_FINDINGS_STORE_PATH", "/var/lib/lhrad/findings.sqlite"))
    app.include_router(create_findings_router(app.state.findings_store))

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "priorityStoreSize": app.state.store.size()}

    @app.post("/priority", status_code=204)
    async def push_priority(body: PriorityPush) -> None:
        """Orchestrator publishes triage output here. Idempotent per
        studyInstanceUID: a re-fired triage upserts the current score.

        TODO(M3): authenticate this endpoint. Currently open — internal on the
        docker-compose network only. When the API moves off the internal network
        add a shared-secret header check here."""
        app.state.store.put(
            body.studyInstanceUID, body.workflowId,
            body.priorityTier, body.priorityScore, now_iso(),
        )

    @app.get("/worklist")
    async def worklist() -> dict:
        """Return studies sorted by orchestrator priority, annotated with
        assignment. See the module docstring for the response shape."""
        try:
            studies = await app.state.orthanc.list_completed_studies()
        except Exception as e:  # noqa: BLE001 — Orthanc down: fail loud, not silent
            raise HTTPException(status_code=503,
                                detail=f"Orthanc unreachable: {e}") from e

        priorities = app.state.store.all()   # single query, no N+1
        items = []
        for study in studies:
            uid = study.get("studyInstanceUID", "")
            prio = priorities.get(uid)
            assignment = await app.state.assignment.get(uid)
            items.append({
                **study,
                "priorityTier":  (prio or {}).get("priorityTier", _DEFAULT_TIER),
                "priorityScore": (prio or {}).get("priorityScore", _DEFAULT_SCORE),
                "workflowId":    (prio or {}).get("workflowId"),
                "assignment":    assignment,
            })

        items.sort(key=lambda it: (
            _TIER_RANK.get(it["priorityTier"], 99),
            -int(it["priorityScore"]),
            # A missing/empty studyDate must sort LAST within its tier, not first.
            # Ascending "" would otherwise rank an undated study as the oldest case
            # and float it above genuinely old stat studies.
            it.get("studyDate") or "99999999",
        ))

        return {"items": items, "generatedAt": now_iso()}

    return app


# Uvicorn entrypoint (`uvicorn main:app` per the other agents' Dockerfiles).
app = create_app()
