"""Event ingress (FastAPI): Orthanc stable-study webhook + RIS DiagnosticReport poller.

Owner: Pranathi (lead). Trigger map: ARCHITECTURE.md
- POST /webhooks/orthanc : starts one StudyWorkflow per new stable study.
- background poller       : detects RIS sign-off and signals the waiting workflow.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from temporalio.client import Client

from radagent_common import validate_against, paths
from radagent_common.tracing import now_iso, new_trace_id, new_span_id
from .state import TASK_QUEUE
from .workflow import StudyWorkflow
from . import activities

TEMPORAL_TARGET = os.environ.get("TEMPORAL_TARGET", "temporal:7233")
POLL_INTERVAL_S = int(os.environ.get("RIS_POLL_INTERVAL_S", "30"))

_client: Client | None = None


async def _temporal() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(TEMPORAL_TARGET)
    return _client


def _build_study_context(event: dict) -> dict:
    """Map an Orthanc event to a (schema-valid) StudyContext.

    TODO(M1): resolve patient.fhirPatientId + order via fhir2 from the accession number.
    For M0 we emit placeholders so the workflow can start end-to-end.
    """
    wf_id = f"wf_{event['orthancStudyId']}"
    return {
        "schemaVersion": "1.0.0",
        "workflowId": wf_id,
        "study": {
            "studyInstanceUID": event["studyInstanceUID"],
            "accessionNumber": event.get("accessionNumber"),
            "orthancStudyId": event["orthancStudyId"],
            "modality": event["modality"],
        },
        "patient": {"fhirPatientId": "Patient/UNRESOLVED"},  # TODO(M1): fhir2 lookup
        "order": {},
        "meta": {
            "traceId": new_trace_id(),
            "spanId": new_span_id(),
            "emittedAt": now_iso(),
            "source": "orchestrator.ingress",
        },
    }


async def _ris_poller() -> None:
    """Poll fhir2 for finalized reports and signal matching workflows."""
    cursor = now_iso()
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        try:
            reports = await activities.poll_finalized_reports(cursor)
        except NotImplementedError:
            continue  # M0: fhir2 client not wired yet
        except Exception:  # noqa: BLE001 - keep the loop alive
            continue
        client = await _temporal()
        for rep in reports:
            wf_id = _workflow_id_for_report(rep)
            if not wf_id:
                continue
            handle = client.get_workflow_handle(wf_id)
            await handle.signal(StudyWorkflow.report_finalized, rep)
            cursor = rep.get("lastUpdatedCursor", cursor)


def _workflow_id_for_report(report: dict) -> str | None:
    """Map a finalized DiagnosticReport back to its workflow id.

    TODO(M1): join on serviceRequestRef / accession -> workflowId (persist the mapping
    when the workflow starts). Returns None until implemented.
    """
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    poller = asyncio.create_task(_ris_poller())
    yield
    poller.cancel()


app = FastAPI(title="LH-Radiology Orchestrator Ingress", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhooks/orthanc")
async def orthanc_webhook(event: dict) -> dict:
    try:
        validate_against(event, paths.contracts_dir() / "events" / "orthanc-stable.schema.json")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(e))

    ctx = _build_study_context(event)
    client = await _temporal()
    await client.start_workflow(
        StudyWorkflow.run,
        ctx,
        id=ctx["workflowId"],
        task_queue=TASK_QUEUE,
    )
    return {"started": ctx["workflowId"]}
