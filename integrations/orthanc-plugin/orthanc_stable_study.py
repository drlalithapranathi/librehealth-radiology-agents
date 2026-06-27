"""Orthanc Python plugin — fires the orchestrator webhook when a study becomes stable.

Deploy: load via Orthanc's Python plugin (configuration "PythonScript").
Fallback: if the Python plugin is awkward in your deployment, the same OnStableStudy
hook can be done in Lua (see Orthanc docs) POSTing the identical JSON body.

Owner: Parvati. Trigger map: ARCHITECTURE.md
"""
import json
import os

import orthanc  # provided by the Orthanc Python plugin runtime
import urllib.request

ORCH_WEBHOOK = os.environ.get("ORCH_WEBHOOK_URL", "http://orchestrator:8090/webhooks/orthanc")


def _post(payload: dict) -> None:
    req = urllib.request.Request(
        ORCH_WEBHOOK, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=10)


def OnStableStudy(study_id, tags, metadata):
    # Build the OrthancStableStudyEvent (contracts/events/orthanc-stable.schema.json)
    payload = {
        "schemaVersion": "1.0.0",
        "eventType": "orthanc.study.stable",
        "orthancStudyId": study_id,
        "studyInstanceUID": tags.get("StudyInstanceUID", ""),
        "modality": tags.get("ModalitiesInStudy", tags.get("Modality", "")),
        "accessionNumber": tags.get("AccessionNumber", ""),
        "occurredAt": metadata.get("LastUpdate", ""),
    }
    try:
        _post(payload)
    except Exception as e:  # noqa: BLE001 - never crash the PACS on a webhook failure
        orthanc.LogError(f"orchestrator webhook failed: {e}")


orthanc.RegisterOnStableStudyCallback(OnStableStudy)
