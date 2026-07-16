"""Study -> subspecialty routing for on-call paging (#58). Owner: Pranathi (lead).

"WHICH on-call rota should hear about this critical finding?" The classifier decides how urgent a
result is; this module decides who its page is aimed at. Both on-call paths (the no-requester
fallback in comms.dispatch and comms.escalate) used to search the directory with no specialty and
take whoever came back first, so a critical intracranial finding could page the on-call
mammographer -- and the ledger would then record that as a delivered communication.

The mapping itself is clinical config, not code: it lives in specialty-routing.yaml beside this
module (CI-validated against contracts/specialty-routing.schema.json), and a deployment edits that
file or points SPECIALTY_ROUTING_PATH at its own. Read fresh per call, no cache, for the same
reason the orchestrator reads its escalation policy fresh per gate entry: a config edit (or a
re-pointed path) takes effect without a restart.

A routing-config disaster must not silence a critical-result page: any load failure degrades to
"no specialty" (the unnarrowed directory search this agent always did) and is logged loudly,
rather than failing the dispatch and leaving the workflow retrying with no page sent at all.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

_log = logging.getLogger("agents.communications.routing")

# The two directions the out-of-specialty fallback can fail in (issue #58 item 3). This default is
# a starting point, not a ruling -- the dial is in the YAML precisely so the PI can set it.
FALLBACK_ANY_ON_CALL = "any-on-call"  # page whoever is on call; the record says it was the wrong someone
FALLBACK_NONE = "none"                # page nobody; the miss is recorded (SKIPPED / escalated:false), nothing re-pages


def _routing_path() -> Path:
    """Env override -> the in-repo default (baked into the agent image)."""
    default = Path(__file__).resolve().parent / "specialty-routing.yaml"
    return Path(os.environ.get("SPECIALTY_ROUTING_PATH", default))


def _config() -> dict:
    try:
        with _routing_path().open() as f:
            data = yaml.safe_load(f)
    except Exception as e:  # noqa: BLE001 -- see the module docstring: degrade, never block a page
        _log.error("specialty routing config unreadable (%s); on-call searches run unnarrowed", e)
        return {}
    if not isinstance(data, dict):
        _log.error("specialty routing config is not a mapping; on-call searches run unnarrowed")
        return {}
    return data


def derive_specialty(study: dict) -> str | None:
    """The subspecialty code for a study, from its modality and description -- or None.

    Rules are evaluated in file order and the FIRST match wins; a rule matches when the study's
    modality is listed in its `modalities` or its studyDescription contains any of its `keywords`
    (both case-insensitive). None means "no rule matched": the on-call search runs unnarrowed,
    exactly the pre-#58 behaviour, so a site with a single general rota is unaffected.
    """
    modality = (study.get("modality") or "").upper()
    description = (study.get("studyDescription") or "").lower()
    try:
        for rule in _config().get("rules") or []:
            if modality and modality in (m.upper() for m in rule.get("modalities") or []):
                return rule["specialty"]
            # `k and` guards a live-edited table with an empty keyword: "" is a substring of
            # every description, which would route ALL studies here (CI's minLength only
            # validates the in-repo file).
            if description and any(k and k.lower() in description
                                   for k in rule.get("keywords") or []):
                return rule["specialty"]
    except Exception as e:  # noqa: BLE001 -- a malformed rule in a live-edited table; CI validates
        # the in-repo file, so degrade here rather than fail the dispatch (module docstring).
        _log.error("specialty routing table malformed (%s); on-call searches run unnarrowed", e)
    return None


def out_of_specialty_fallback() -> str:
    """The configured dial for "a specialty was derived but nobody in it is on call".

    An unknown value falls back to FALLBACK_ANY_ON_CALL rather than raising: the in-repo file is
    CI-validated, so this only guards a live edit or SPECIALTY_ROUTING_PATH override -- and of the
    two failure directions, the one where someone hears the page is the one a typo should buy.
    """
    value = _config().get("outOfSpecialtyFallback", FALLBACK_ANY_ON_CALL)
    if value not in (FALLBACK_ANY_ON_CALL, FALLBACK_NONE):
        _log.error("unknown outOfSpecialtyFallback %r; treating as %r", value, FALLBACK_ANY_ON_CALL)
        return FALLBACK_ANY_ON_CALL
    return value
