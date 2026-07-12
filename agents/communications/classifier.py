"""ACR critical-results classifier (#52, MR 3). Owner: Pranathi (lead).

"How urgent is this, and how long may the physician take to acknowledge it?" The answer drives
everything downstream: whether we open an ack clock at all, how long that clock runs, and how
loudly we escalate when it expires.

v1 is DETERMINISTIC and derives the category from signals the pipeline already computed --
Impression Generation's `criticalFlags` (#16/#26) and Report Verification's status. It does not
read the narrative. That is deliberate: CritCom's real classifier is a Gemini call, and an LLM in
the v1 path would mean a non-deterministic, unbudgeted, unavailable-offline dependency inside the
COMMUNICATE step. M3 swaps `classify()` for the Gemini implementation behind this same signature
-- exactly the pattern interpretation-assistant/registry.py uses for its tools.

The ACR categories (ACR Actionable Reporting work group) and the timeouts they imply:
  Cat1  immediate  -- a life-threatening finding; contact within 60 minutes.
  Cat2  urgent     -- contact within 24 hours.
  Cat3  routine    -- normal reporting workflow; no ack clock.
  None  no critical finding.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum


class ACRCategory(str, Enum):
    CAT1 = "Cat1"
    CAT2 = "Cat2"
    CAT3 = "Cat3"
    NONE = "None"


# Minutes the physician has to acknowledge, per category. Env-overridable per deployment (a
# tertiary centre's Cat1 window is not a rural clinic's). Cat3/None open no ack clock at all.
_DEFAULT_ACK_MINUTES = {ACRCategory.CAT1: 60, ACRCategory.CAT2: 1440}


def _ack_minutes(category: ACRCategory) -> int | None:
    default = _DEFAULT_ACK_MINUTES.get(category)
    if default is None:
        return None
    return int(os.environ.get(f"CRITCOM_{category.value.upper()}_ACK_TIMEOUT_MINUTES", default))


@dataclass(frozen=True)
class Classification:
    category: ACRCategory
    finding: str          # one-line summary; becomes the Communication's payload
    ack_minutes: int | None

    @property
    def is_critical(self) -> bool:
        """Cat1/Cat2 are the closed-loop categories: someone must acknowledge, on a clock."""
        return self.category in (ACRCategory.CAT1, ACRCategory.CAT2)


def classify(impression: dict, verification: dict) -> Classification:
    """Categorise a signed study from the pipeline's own derived signals.

    - A critical flag from Impression Generation -> Cat1. The impression only raises a flag for
      the findings on the critical list (pneumothorax, dissection, ...), all of which are
      immediate-contact findings, so there is no honest way for v1 to sort them into Cat1 vs Cat2
      without reading the narrative. Erring toward Cat1 costs a faster page; erring the other way
      costs an hour on a tension pneumothorax. Take the faster page.
    - Verification FAILed and asked for a human -> Cat2. Something is wrong with the report itself
      (an uncommunicated critical, a missing section). A human must look, but it is not the same
      as a confirmed life-threatening finding.
    - Otherwise -> None. Routine result, no ack clock; the report still posts to the EHR inbox.
    """
    flags = impression.get("criticalFlags") or []
    if flags:
        labels = ", ".join(f["label"] for f in flags if f.get("label")) or "critical finding"
        return Classification(ACRCategory.CAT1, labels, _ack_minutes(ACRCategory.CAT1))

    if verification.get("verificationStatus") == "FAIL" or verification.get("requiresHumanReview"):
        issues = verification.get("issues") or []
        first = (issues[0] or {}).get("message") if issues else None
        return Classification(
            ACRCategory.CAT2,
            first or "report verification failed; human review required",
            _ack_minutes(ACRCategory.CAT2),
        )

    return Classification(ACRCategory.NONE, "no critical finding", None)
