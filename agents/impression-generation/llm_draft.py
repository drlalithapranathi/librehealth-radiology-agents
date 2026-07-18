"""LLM-authored impression prose (#77). Owner: Chaitra.

Prose ONLY: `impressionText` + `recommendations`. `criticalFlags` (and `structuredFindings`,
derived from it) stay a deterministic derivation from handler.py's keyword/negation scan --
never influenced by this module. That split is #78's whole point: criticality pages physicians
via CritCom and must stay auditable and testable without a model, so this file only ever
CONSUMES an already-computed `critical_flags` list -- it never derives or overrides one.

Config-gated + best-effort: unset IMPRESSION_LLM_BASE_URL/MODEL -> feature off, handler.py's
deterministic template is the default AND the fallback. ANY failure here -- network, timeout,
malformed output, a half-set misconfiguration, or prose that contradicts the confirmed critical
flags -- returns None rather than raising. The read must never be stranded on a hosting choice
the PI has not made yet.

Hosting-agnostic by design: speaks the OpenAI chat-completions HTTP shape that vLLM, Ollama, and
most cloud providers all implement, so the PI's hosting decision (local open-weights vs. a
DUA-compliant cloud service) becomes a config value -- base URL + model name -- not a code branch.

Never logs prompt or response CONTENT -- only exception class/message text and HTTP status
codes, matching fhir_client.py's "host only, never the clinical text" logging discipline. The
malformed-output ValueErrors raised below carry deliberately content-free reason strings (which
check failed, e.g. "empty impressionText"), so logging their message is safe and is what makes a
malformed-vs-empty-vs-flag-mismatch rejection distinguishable in production logs.
"""
from __future__ import annotations
import json
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

_log = logging.getLogger(__name__)
_warned_misconfigured = False

_DEFAULT_TIMEOUT_SECONDS = 12.0

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_plaintext_remote(base_url: str) -> bool:
    """Plaintext `http` to a non-loopback host: the transport that exposes anything sent on it."""
    parsed = urlparse(base_url)
    return parsed.scheme != "https" and (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS


def _egress_transport_is_secure(base_url: str) -> bool:
    """May we POST the report conclusion + EHR context to this LLM base URL (#77)?

    The chat-completions request body carries clinical text -- the report conclusion and the EHR
    problems/labs summary -- to whatever IMPRESSION_LLM_BASE_URL names. Over plaintext `http` to a
    remote host that (plus any Authorization key) is exposed on the wire, and for MIMIC content
    that is a DUA problem. So mirror fhir_client's write guard exactly: refuse plaintext-remote
    UNLESS the target is loopback (local model / unit tests) or the deployment has accepted the risk
    on a trusted internal network via IMPRESSION_LLM_ALLOW_INSECURE. `https` is always fine. The
    truthy set matches FHIR2_ALLOW_INSECURE_WRITE so an operator's opt-in behaves identically here.
    """
    if not _is_plaintext_remote(base_url):
        return True  # https, or a loopback host
    return os.environ.get("IMPRESSION_LLM_ALLOW_INSECURE", "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class LLMDraft:
    impression_text: str
    recommendations: list[str]


def _timeout_seconds() -> float:
    raw = os.environ.get("IMPRESSION_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        return float(raw)
    except ValueError:
        _log.warning(
            "IMPRESSION_LLM_TIMEOUT_SECONDS=%r is not a number; using the %.0fs default",
            raw, _DEFAULT_TIMEOUT_SECONDS,
        )
        return _DEFAULT_TIMEOUT_SECONDS


def _configured() -> tuple[str, str, str, float] | None:
    """(base_url, model, api_key, timeout), or None to fall back to the deterministic template.

    Unset (neither var) -> off, silently -- that's the default. Half-set -> a misconfiguration:
    warned ONCE (mirrors radagent_common.tracing's _warned_missing pattern) then treated as off,
    never raised -- this sits on the read path and must never strand it on a config typo."""
    base_url = os.environ.get("IMPRESSION_LLM_BASE_URL", "").strip()
    model = os.environ.get("IMPRESSION_LLM_MODEL", "").strip()
    if not base_url and not model:
        return None
    if bool(base_url) != bool(model):
        global _warned_misconfigured
        if not _warned_misconfigured:
            _warned_misconfigured = True
            _log.warning(
                "IMPRESSION_LLM_BASE_URL and IMPRESSION_LLM_MODEL must both be set; "
                "falling back to the deterministic template."
            )
        return None
    api_key = os.environ.get("IMPRESSION_LLM_API_KEY", "").strip()
    return base_url, model, api_key, _timeout_seconds()


def _labels_as_text(finding_labels: str | list[str] | tuple[str, ...] | set[str]) -> str:
    """`finding_labels` is a lowercased str today; #78 (in flight) changes its producer to return
    list[str]. This handler-side value is never touched by this module's caller (handler.py's
    scan block is #78's territory), so normalize defensively here instead of assuming either
    shape survives untouched across merge order."""
    if isinstance(finding_labels, str):
        return finding_labels
    return " ".join(str(v) for v in finding_labels if v)


def _summarize_ehr_context(ehr_context: dict) -> str:
    """An explicit allowlist -- never a blanket json.dumps(ehr_context). `contrastFlags` and
    `medicationFlags` are additionalProperties:true in contracts/skills/ehr.schema.json, so a raw
    dump would silently forward whatever a future EHR Assistant change adds there to an external
    endpoint, unreviewed."""
    problems = ", ".join(
        p.get("display") or p.get("code", "") for p in ehr_context.get("activeProblems", []) if p
    )
    labs = ", ".join(
        f"{lab.get('display') or lab.get('code', '')}: {lab.get('value', '')} {lab.get('unit', '')}".strip()
        for lab in ehr_context.get("relevantLabs", []) if lab
    )
    parts = []
    if problems:
        parts.append(f"Active problems: {problems}.")
    if labs:
        parts.append(f"Relevant labs: {labs}.")
    return " ".join(parts)


_SYSTEM_PROMPT = (
    "You are drafting the prose portion of a radiology impression. You will be given the "
    "confirmed critical findings -- already decided by a separate deterministic process -- plus "
    "supporting context. Write natural clinical prose that is consistent with, and does not "
    "contradict or invent findings beyond, the confirmed critical findings and the conclusion "
    "text given. Respond with ONLY a JSON object of the shape "
    '{"impressionText": "...", "recommendations": ["...", "..."]} -- no markdown, no commentary.'
)


def _build_prompt(*, conclusion: str, labels_text: str, critical_flags: list[dict], ehr_summary: str) -> str:
    flag_labels = ", ".join(f["label"] for f in critical_flags if f.get("label")) or "none"
    lines = [
        f"Confirmed critical findings (authoritative, do not contradict): {flag_labels}",
        f"Report conclusion: {conclusion or '(none available)'}",
        f"AI finding labels: {labels_text or '(none)'}",
    ]
    if ehr_summary:
        lines.append(ehr_summary)
    return "\n".join(lines)


async def _chat_completion(base_url: str, model: str, api_key: str, timeout: float, prompt: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _parse_draft(content: str, critical_flags: list[dict]) -> LLMDraft:
    """Raises on anything unusable; draft_impression() turns every raise into a None. Strips a
    ```json fence defensively -- response_format support varies across OpenAI-compatible
    backends, and some wrap JSON in a code fence regardless of the hint."""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[len("json"):]
        text = text.strip()
    parsed = json.loads(text)
    impression_text = parsed["impressionText"]
    recommendations = parsed["recommendations"]
    if not isinstance(impression_text, str) or not impression_text.strip():
        raise ValueError("empty impressionText")
    if not isinstance(recommendations, list) or not recommendations or not all(
        isinstance(r, str) and r.strip() for r in recommendations
    ):
        raise ValueError("malformed or empty recommendations")
    # Criticality is never the LLM's call (#78 owns that derivation) -- so if a critical flag was
    # already confirmed, the prose must at least name it. A conservative reject-and-fall-back-to-
    # template here (same bias as #78's negation scanners: an over-flag is tolerated, a silent
    # miss is not) beats risking prose that reads as reassuring next to a real critical finding.
    # Verbatim substring match on purpose: prose that says "PTX" for a "pneumothorax" flag is
    # rejected and falls back to the template. That is the conservative direction (a false reject
    # costs deterministic prose, a false accept could read as reassuring next to a real finding), so
    # the strictness is deliberate, not a gap.
    flag_labels = [f["label"].lower() for f in critical_flags if f.get("label")]
    if flag_labels and not any(label in impression_text.lower() for label in flag_labels):
        raise ValueError("prose does not reference any confirmed critical flag")
    return LLMDraft(
        impression_text=impression_text.strip(),
        recommendations=[r.strip() for r in recommendations],
    )


async def draft_impression(
    *, conclusion: str, finding_labels: str | list[str], critical_flags: list[dict], ehr_context: dict,
) -> LLMDraft | None:
    """The sole entry point. Returns None -- never raises -- when the LLM path is unset,
    misconfigured, or fails/produces something unusable for any reason; handler.py falls back to
    the deterministic template on None, exactly as it already does for a fhir2 fetch failure."""
    config = _configured()
    if config is None:
        return None
    base_url, model, api_key, timeout = config

    # No clinical text (report conclusion + EHR context) over plaintext HTTP to a remote host (#77).
    # Best-effort like every other failure here: skip to the deterministic template rather than
    # raise, so a transport misconfiguration never strands the read -- but the PHI never leaves.
    if not _egress_transport_is_secure(base_url):
        _log.warning(
            "impression LLM draft skipped: refusing to POST report + EHR context over plaintext "
            "HTTP to non-loopback host %s; use an https base URL, or set "
            "IMPRESSION_LLM_ALLOW_INSECURE=1 for a trusted internal network",
            urlparse(base_url).hostname,
        )
        return None
    if _is_plaintext_remote(base_url):
        # Proceeding only because of the insecure opt-in; leave an audit trail that clinical text
        # went out in cleartext on this hop. Host only -- never the report or EHR content.
        _log.warning(
            "impression LLM draft proceeding over PLAINTEXT http to %s under "
            "IMPRESSION_LLM_ALLOW_INSECURE: report conclusion + EHR context are in cleartext on this hop",
            urlparse(base_url).hostname,
        )

    prompt = _build_prompt(
        conclusion=conclusion,
        labels_text=_labels_as_text(finding_labels),
        critical_flags=critical_flags,
        ehr_summary=_summarize_ehr_context(ehr_context),
    )
    try:
        content = await _chat_completion(base_url, model, api_key, timeout, prompt)
        return _parse_draft(content, critical_flags)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as e:
        _log.warning("impression LLM draft skipped: unusable IMPRESSION_LLM_BASE_URL (%s)", e.__class__.__name__)
    except httpx.HTTPStatusError as e:
        _log.warning("impression LLM draft failed: HTTP %s", e.response.status_code)
    except (httpx.TimeoutException, httpx.HTTPError) as e:
        _log.warning("impression LLM draft failed: %s", e.__class__.__name__)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        # e's message is safe to log: JSONDecodeError describes a syntax position, KeyError names
        # a missing key, and the ValueErrors raised in _parse_draft are hand-written, content-free
        # reason strings -- none of them carry model output or report text.
        _log.warning("impression LLM draft malformed: %s: %s", e.__class__.__name__, e)
    except Exception as e:  # noqa: BLE001 - never-raises backstop; this path is advisory only
        _log.warning("impression LLM draft unexpected failure: %s", e.__class__.__name__)
    return None
