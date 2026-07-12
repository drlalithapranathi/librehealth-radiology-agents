"""Thin HTTP client for the Worklist API (see integrations/worklist-api/main.py).

The Worklist API is a plain FastAPI service (not an A2A agent) — it exposes
POST /priority for the orchestrator's publish channel and GET /worklist for
OHIF. This module is a per-external-system client, matching the pattern of
orthanc_client.py and fhir_client.py.

Best-effort by design (READ THIS): the publish is *visibility*, not
*correctness*. If the Worklist API is unreachable when the orchestrator
publishes a triage result, the study still gets interpreted, reported, and
signed — it just doesn't show priority-ordered in OHIF until the next publish
succeeds. So this helper NEVER raises; a failed publish is a WARNING log line
and a falsy return value, not an exception that would fail the Temporal
activity and block the workflow.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

_log = logging.getLogger(__name__)

# Short timeout: this call is on the readiness-for-read path; a slow publish
# should not stall the transition. Temporal activity retries with backoff give
# us the eventual-consistency safety net.
_DEFAULT_TIMEOUT = 5.0


async def publish_priority(
    base_url: str,
    study_instance_uid: str,
    workflow_id: str,
    priority_tier: str,
    priority_score: int,
    timeout: float = _DEFAULT_TIMEOUT,
) -> bool:
    """POST the study's triage priority to the Worklist API.

    Returns True on 2xx, False on any error (network, timeout, non-2xx). Never
    raises. The caller (publish_priority_activity) uses the return value only
    for its structured log line; there is no branching on it.

    Payload shape matches integrations/worklist-api/main.py's PriorityPush
    pydantic model — a Worklist API-side 422 (schema violation) means the
    caller passed a bad tier or score and is a real bug worth logging, but
    still not fatal to the workflow.
    """
    url = base_url.rstrip("/") + "/priority"
    payload = {
        "studyInstanceUID": study_instance_uid,
        "workflowId": workflow_id,
        "priorityTier": priority_tier,
        "priorityScore": priority_score,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.post(url, json=payload)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        _log.warning(
            "worklist publish failed (network) wf=%s study=%s: %s",
            workflow_id, study_instance_uid, e,
        )
        return False

    if resp.status_code >= 400:
        # 422 in particular is a caller-side bug (bad tier/score) — log at
        # WARNING with the response body for triage without failing the workflow.
        _log.warning(
            "worklist publish rejected wf=%s study=%s: HTTP %s %s",
            workflow_id, study_instance_uid, resp.status_code, _brief(resp),
        )
        return False
    return True


def _brief(resp: httpx.Response) -> str:
    """First 200 chars of the response body — enough to see a FastAPI 422 detail
    without spamming the log with a full stack trace on a large error page."""
    try:
        return (resp.text or "")[:200]
    except Exception:  # noqa: BLE001 — defensive; log helper must never raise
        return "<unreadable>"
