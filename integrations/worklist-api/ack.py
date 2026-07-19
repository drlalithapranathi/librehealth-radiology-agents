"""The explicit ack surface (#79): the link a referring physician taps to close the loop.

The chart notification (the comms agent's ehr-inbox write) carries
`{CRITCOM_ACK_BASE_URL}/ack/{task_id}?sig={hmac}`. This module serves that route. Three checks,
in a deliberate order:

1. **Signature first**, before any auth challenge: a forged or enumerated task id is a 403 and
   never even gets a password prompt, so credentials are never solicited by an illegitimate link
   (`radagent_common.ack_link` holds the signing rationale).
2. **Identity is the human, not the link.** Possession of a URL is not "Dr X acknowledged": the
   caller authenticates with their own OpenMRS account (HTTP Basic), resolved through
   `/ws/rest/v1/session` — the same identity OpenMRS itself would report. No new accounts, no
   password handling beyond passing Basic through to OpenMRS.
3. **The ledger Task closes with WHO on it** (`complete_ack_task`: status COMPLETED + a note
   naming the acknowledger). `comms.checkAck` then reports COMPLETED and the orchestrator's
   escalation never fires — the run-book's "acknowledged in time" arc.

A GET with a side effect, deliberately: the whole point is ONE tap on a paged phone. The HMAC
gate means no third party can construct the URL, and the Basic gate means the tap is attributed;
an idempotent re-tap (or a link-previewing mail client that somehow acquired both the link and
credentials) lands on the already-acknowledged page without re-writing anything beyond a repeat
COMPLETED — never a duplicated loop.

Kept as a sibling module (the `assignment.py`/`store.py` pattern) so `main.py` stays the thin
app factory. Inert until a deployment sets CRITCOM_ACK_HMAC_SECRET: without it every signature
verification fails closed and no links are ever minted on the producer side.
"""
from __future__ import annotations

import base64
import html
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from radagent_common.ack_link import verify_ack_task
from radagent_common.fhir_models import TaskStatus
from radagent_common.openmrs_rest import rest_base_url
from radagent_common.tracing import now_iso

_log = logging.getLogger("worklist-api.ack")

_ACKED = (TaskStatus.COMPLETED, TaskStatus.ACCEPTED)


class OpenmrsIdentity:
    """WHO is acknowledging, per OpenMRS. `whoami` returns a display string for an authenticated
    user ("display (uuid)"), or None for bad credentials -- deliberately no distinction between
    unknown user and wrong password."""

    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or rest_base_url()).rstrip("/")
        self._timeout = timeout

    async def whoami(self, username: str, password: str) -> str | None:
        async with httpx.AsyncClient(timeout=self._timeout, auth=(username, password)) as c:
            r = await c.get(f"{self.base_url}/session")
            if r.status_code != 200:
                return None
            body = r.json()
            if not body.get("authenticated"):
                return None
            user = body.get("user") or {}
            display = user.get("display") or username
            uuid = user.get("uuid") or ""
            return f"{display} ({uuid})" if uuid else display


def _challenge() -> Response:
    """401 + a Basic challenge so a phone browser opens its native login prompt."""
    return Response(
        status_code=401,
        content="Sign in with your OpenMRS account to acknowledge this result.",
        headers={"WWW-Authenticate": 'Basic realm="LH-Radiology critical-result acknowledgement"'},
    )


def _page(title: str, lines: list[str]) -> HTMLResponse:
    body = "".join(f"<p>{html.escape(line)}</p>" for line in lines if line)
    return HTMLResponse(
        f"<!doctype html><html><head><meta name=\"viewport\" "
        f"content=\"width=device-width, initial-scale=1\"><title>{html.escape(title)}</title>"
        f"</head><body style=\"font-family: sans-serif; max-width: 30em; margin: 3em auto;\">"
        f"<h1 style=\"font-size:1.2em\">{html.escape(title)}</h1>{body}</body></html>"
    )


def create_ack_router(ledger, identity: OpenmrsIdentity) -> APIRouter:
    router = APIRouter()

    @router.get("/ack/{task_id}")
    async def acknowledge(task_id: str, request: Request, sig: str = "") -> Response:
        # 1. The link itself must be genuine -- BEFORE any credential prompt.
        if not verify_ack_task(task_id, sig):
            raise HTTPException(status_code=403, detail="invalid acknowledgement link")

        # 2. The human. fastapi's HTTPBasic dependency is skipped on purpose: it cannot order
        # itself after the signature check, and the challenge must not fire for forged links.
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("basic "):
            return _challenge()
        try:
            username, _, password = base64.b64decode(auth[6:]).decode().partition(":")
        except Exception:
            return _challenge()
        who = await identity.whoami(username, password)
        if who is None:
            return _challenge()

        # 3. The loop.
        try:
            task = await ledger.get_task(task_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (404, 410):
                raise HTTPException(status_code=404, detail="unknown acknowledgement task") from e
            raise

        finding = None
        if task.focus and task.focus.reference:
            try:
                comm = await ledger.get_communication(task.focus.reference.split("/")[-1])
                finding = comm.finding_summary
            except Exception:  # noqa: BLE001 -- the page must not fail over its garnish
                finding = None

        if task.status in _ACKED:
            return _page("Already acknowledged", [
                f"Finding: {finding}" if finding else "",
                "This critical result was already acknowledged; nothing further is needed.",
            ])

        await ledger.complete_ack_task(task_id, acknowledged_by=who, at_iso=now_iso())
        _log.info("ack task %s completed by %s", task_id, who)
        return _page("Critical result acknowledged", [
            f"Finding: {finding}" if finding else "",
            f"Recorded as acknowledged by {who}.",
            "The care team's escalation clock for this result is now closed.",
        ])

    return router
