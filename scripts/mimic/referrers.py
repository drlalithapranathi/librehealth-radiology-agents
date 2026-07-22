"""Demo referring-physician roster + deterministic study assignment (#76, build item 1).

For the referring-physician showcase arc, each cohort order must be REQUESTED by a real provider,
not the ETL's admin account: fhir2 surfaces `orders.orderer` as `ServiceRequest.requester`,
`agents.communications.tools.resolve_ordering_provider` reads that reference verbatim, and the
critical-result notification then names that physician and lands on the ordering patient's chart
for them to acknowledge (docs/ehr-inbox-notification.md, #76).

This module is the PURE half -- WHO the demo physicians are and WHICH one ordered a given study.
The write half (get-or-create the OpenMRS Provider + login User) is
`omrs_client.OmrsClient.ensure_referring_provider`; the wiring is `load_cohort.load_study`.

Assignment is deterministic on subject_id so every study of one patient shares one ordering
physician -- a patient followed by a single referring provider is the clinically honest demo, and
determinism keeps a re-run of the idempotent ETL (#68) assigning the same requester rather than
churning a patient across referrers.
"""
from __future__ import annotations

import re

# One entry per participating referring physician. Small on purpose: the demo needs a handful of
# distinct ordering providers, not a real directory. `username` is the OpenMRS login the physician
# uses at the ack surface (#86); `given`/`family` name the Provider that fhir2 emits as the
# requester; `gender` is required by the OpenMRS person create.
REFERRERS: list[dict] = [
    {"username": "dr.reyes", "given": "Marisol", "family": "Reyes", "gender": "F"},
    {"username": "dr.okafor", "given": "Chidi", "family": "Okafor", "gender": "M"},
    {"username": "dr.novak", "given": "Tomas", "family": "Novak", "gender": "M"},
]


def _subject_bucket(subject_id: str, n: int) -> int:
    """Stable bucket in [0, n) for a subject_id. Digits only, so 'p10000032' and '10000032' land
    the same (the fetch tool already treats the `p` prefix as optional); a subject with no digits
    falls back to a character-sum so it still assigns deterministically instead of raising."""
    digits = re.sub(r"\D", "", str(subject_id or ""))
    key = int(digits) if digits else sum(map(ord, str(subject_id or "")))
    return key % n


def assign(subject_id: str, roster: list[dict] | None = None) -> dict:
    """The referring physician who ordered this patient's studies -- deterministic on subject_id."""
    roster = roster if roster is not None else REFERRERS
    if not roster:
        raise ValueError("empty referrer roster: cannot assign an ordering provider")
    return roster[_subject_bucket(subject_id, len(roster))]
