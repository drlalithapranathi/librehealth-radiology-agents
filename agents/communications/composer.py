"""Optional LLM prose for the physician-facing notification (CritCom protocol format).

The deterministic layer stays authoritative for everything that pages: WHO is notified, the ACR
category, the ack deadline, and escalation are classifier/rule decisions (the #78 thesis -- the
safety trigger must be auditable without a model). This module upgrades exactly ONE string: the
Communication payload text a physician reads. The category is PRE-DECIDED and passed in; the
model is told not to re-classify.

Fail-safe by construction: flag off (the default), no key, timeout, transport error, HTTP error,
or an empty/malformed reply all return None, and the caller falls back to the deterministic
one-line summary. Paging never waits on and never fails because of the LLM -- the composer gets
one bounded attempt (COMMS_LLM_TIMEOUT_SECONDS, default 5) inside a dispatch that was going to
send either way.

Lean-reference prompt (golden rule 2 applied to an EXTERNAL model): the prompt carries the ACR
category, the finding label, and the ack window -- never the report narrative, never patient or
order identifiers. Widening it to the narrative would send PHI to an external API and needs a
#30-style review first.

GEMINI_API_KEY comes from the operator's environment only (compose passes ${GEMINI_API_KEY:-}
through). It rides the x-goog-api-key header, so no URL or log line ever carries it.
"""
from __future__ import annotations

import logging
import os
import re

import httpx

_log = logging.getLogger("agents.communications.composer")

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_DEFAULT_MODEL = "gemini-2.5-flash-lite"
# Byte-for-byte the family truthy set (FHIR2_ALLOW_INSECURE_WRITE / EHR_INBOX_WRITE_ENABLED /
# IMPRESSION_LLM_ALLOW_INSECURE): two switches with different token sets is an operator trap
# (!73 review, item 3) -- "on" deliberately does NOT enable this one either.
_TRUTHY = {"1", "true", "yes"}

# COMMS_LLM_MODEL rides into the URL PATH; a stray "/" or "?" in a typo'd value would rewrite the
# endpoint (same https host, wrong route). Constrain to plain model-name characters and fall back
# on anything else -- a bad model name must degrade like any other failure, not steer the URL.
_MODEL_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")

# Her CritCom protocol responder, adapted for a pre-decided category: the deterministic classifier
# already chose Cat1/Cat2 and the window, so the model composes the message AROUND that decision
# instead of making it. "Never ask questions" replaces the interactive prompt's clarifying-question
# rule -- there is no conversation here, only one message that must stand on its own.
_PROMPT = """You are CritCom, the radiology critical-results communication specialist.

The deterministic pipeline has ALREADY classified this finding and opened the acknowledgment
clock. Do not re-classify, do not change the category or the window, do not invent clinical
detail beyond the finding label given. You have no patient identifiers and must not fabricate
any. Never ask questions. Output only the message, in exactly this format:

**Critical Results Communication Protocol**

**Finding:** <one-sentence clinical restatement of the finding label>
**ACR Category:** {category} — <one-line reasoning for why this category fits the finding>

**Action plan:**
- The ordering physician has been notified via pager and EHR inbox.
- Acknowledge within {ack_minutes} minutes; an unacknowledged result escalates to the on-call
  provider on a shorter window.

Be concise, clinical, and decisive.

Finding label: {finding}
ACR category (pre-decided): {category}
Acknowledgment window: {ack_minutes} minutes"""


def _enabled() -> bool:
    return os.environ.get("COMMS_LLM_COMPOSER", "").strip().lower() in _TRUTHY


def _contradicts_category(text: str, category: str) -> bool:
    """Does the composed prose name a DIFFERENT ACR category, or fail to name the decided one?

    The category is paging semantics -- it decided the window and the escalation ladder -- so a
    message whose visible text says Cat3 while the clock runs on Cat1 misinforms the physician
    about the urgency of their own deadline. Same precedent as the impression module's
    flag-consistency check (#77): the deterministic layer decided; prose that contradicts the
    decision is rejected and the deterministic fallback goes out instead."""
    lowered = text.lower()
    named = {c for c in ("cat1", "cat2", "cat3") if c in lowered}
    return category.lower() not in named or bool(named - {category.lower()})


def _timeout_seconds() -> float:
    try:
        return float(os.environ.get("COMMS_LLM_TIMEOUT_SECONDS", "5"))
    except ValueError:
        return 5.0


async def compose_notification(*, acr_category: str, finding: str,
                               ack_minutes: int | None) -> str | None:
    """The physician-facing notification text, or None meaning "use the deterministic fallback".

    None is the answer to EVERY failure mode -- this function must never raise into a dispatch."""
    if not _enabled():
        return None
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        # Flag on with no key is a config gap, but the dispatch must still page: fall back loudly.
        _log.warning("COMMS_LLM_COMPOSER is on but GEMINI_API_KEY is unset; using fallback text")
        return None

    model = os.environ.get("COMMS_LLM_MODEL", "").strip() or _DEFAULT_MODEL
    if not _MODEL_TOKEN.fullmatch(model):
        _log.warning("composer fallback: COMMS_LLM_MODEL is not a plain model name; ignoring it")
        return None
    prompt = _PROMPT.format(category=acr_category, finding=finding,
                            ack_minutes=ack_minutes if ack_minutes is not None else 60)
    try:
        async with httpx.AsyncClient(timeout=_timeout_seconds()) as client:
            resp = await client.post(
                _GEMINI_URL.format(model=model),
                headers={"x-goog-api-key": key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
            )
            resp.raise_for_status()
            body = resp.json()
        text = body["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not text:
            return None
        if _contradicts_category(text, acr_category):
            # Content-free by design: log the decided category (a classifier code, not clinical
            # text), never the model's prose.
            _log.warning("composer fallback: prose contradicts the decided ACR category %s",
                         acr_category)
            return None
        return text
    except Exception as exc:  # noqa: BLE001 -- any failure means "fall back", never "fail the page"
        # Exception type + message only; the key lives in a header, never in the URL or str(exc).
        _log.warning("composer fallback: %s: %s", type(exc).__name__, exc)
        return None
