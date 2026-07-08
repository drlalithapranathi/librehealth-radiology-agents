"""Orthanc Python plugin — fires the orchestrator webhook when a study becomes stable.

Deploy: load via Orthanc's Python plugin (configuration "PythonScript").
Fallback: if the Python plugin is awkward in your deployment, the same OnStableStudy
hook can be done in Lua (see Orthanc docs) POSTing the identical JSON body.

Owner: Parvati. Trigger map: ARCHITECTURE.md
"""
import json
import os
from datetime import datetime, timezone

import orthanc  # provided by the Orthanc Python plugin runtime
import urllib.parse
import urllib.request

ORCH_WEBHOOK = os.environ.get("ORCH_WEBHOOK_URL", "http://orchestrator:8090/webhooks/orthanc")


def _now_iso_utc() -> str:
    # RFC 3339 UTC "now", matching the Lua fallback's nowIsoUtc(). Fall-through for
    # occurredAt so the event stays schema-valid when a build omits LastUpdate.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post(payload: dict) -> None:
    # urlopen also dereferences file:/ftp: schemes (Bandit B310 / CWE-939). ORCH_WEBHOOK is
    # operator-configured, but constraining the scheme stops an accidental or tampered
    # non-HTTP URL from ever being fetched.
    if urllib.parse.urlparse(ORCH_WEBHOOK).scheme not in ("http", "https"):
        raise ValueError(f"refusing non-HTTP(S) orchestrator webhook URL: {ORCH_WEBHOOK!r}")
    req = urllib.request.Request(
        ORCH_WEBHOOK, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    # scheme constrained to http(s) above, so urlopen cannot reach file:/ftp:
    urllib.request.urlopen(req, timeout=10)  # nosemgrep


def OnStableStudy(study_id, tags, metadata):
    # Build the OrthancStableStudyEvent (contracts/events/orthanc-stable.schema.json)
    payload = {
        "schemaVersion": "1.0.0",
        "eventType": "orthanc.study.stable",
        "orthancStudyId": study_id,
        "studyInstanceUID": tags.get("StudyInstanceUID", ""),
        "modality": tags.get("ModalitiesInStudy", tags.get("Modality", "")),
        "accessionNumber": tags.get("AccessionNumber", ""),
        "occurredAt": metadata.get("LastUpdate") or _now_iso_utc(),
    }
    try:
        _post(payload)
    except Exception as e:  # noqa: BLE001 - never crash the PACS on a webhook failure
        orthanc.LogError(f"orchestrator webhook failed: {e}")


orthanc.RegisterOnStableStudyCallback(OnStableStudy)
