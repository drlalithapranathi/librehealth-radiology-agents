"""HMAC-signed ack links (#79, the ack surface).

The chart notification (`fhir_client.write_critical_result_notification`) may carry a link the
referring physician taps to acknowledge a critical result. Two properties, split deliberately:

* the SIGNATURE (here) proves the link was minted by this system for exactly this ack Task --
  a forged or enumerated task id never reaches the acknowledge flow;
* IDENTITY is NOT the link. Possession of a URL (forwarded email, shared screen, shoulder-surfed
  phone) must never count as "Dr X acknowledged": the ack endpoint separately authenticates the
  human against OpenMRS (`/ws/rest/v1/session`) and records WHO on the ledger Task.

One env var, `CRITCOM_ACK_HMAC_SECRET`, shared by the link producer (the comms agent's fhir2
write) and the verifier (the worklist-api ack route). With it unset, no links are emitted and the
verifier refuses everything -- the surface simply does not exist until a deployment configures it
(the same inert-by-default posture as EHR_INBOX_WRITE_ENABLED).
"""
from __future__ import annotations

import hashlib
import hmac
import os

_SECRET_ENV = "CRITCOM_ACK_HMAC_SECRET"


def ack_secret() -> str:
    """The shared link secret, '' when the deployment has not configured the ack surface."""
    return os.environ.get(_SECRET_ENV, "")


def sign_ack_task(ack_task_id: str, secret: str | None = None) -> str:
    """The hex signature for one ack Task's link. Raises when no secret is configured or the
    task id is empty -- an unsigned or unanchored link must never be minted silently."""
    key = ack_secret() if secret is None else secret
    if not key:
        raise ValueError(f"no ack-link secret configured ({_SECRET_ENV})")
    if not ack_task_id:
        raise ValueError("refusing to sign an empty ack task id")
    return hmac.new(key.encode(), f"ack::{ack_task_id}".encode(), hashlib.sha256).hexdigest()


def verify_ack_task(ack_task_id: str, sig: str, secret: str | None = None) -> bool:
    """Constant-time check of a presented link signature. False -- never a raise -- for a missing
    secret, empty task id, or empty signature: the caller turns False into a 403, and an
    unconfigured deployment rejects everything."""
    key = ack_secret() if secret is None else secret
    if not key or not ack_task_id or not sig:
        return False
    return hmac.compare_digest(sign_ack_task(ack_task_id, key), sig)
