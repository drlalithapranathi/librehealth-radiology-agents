"""The ACR classifier (#52, MR 3): how urgent, and how long may the physician take to answer?

v1 is deterministic on purpose (see classifier.py). These pin the category boundaries and the ack
windows they imply, because those two numbers are the whole escalation contract: get the category
wrong and a tension pneumothorax waits 24 hours.
"""
from __future__ import annotations

from classifier import ACRCategory, classify

CRITICAL = {"criticalFlags": [{"label": "tension pneumothorax", "severity": "critical"}]}


def test_a_critical_flag_is_cat1_with_a_60_minute_clock():
    """The impression only flags findings on the critical list, and they are all immediate-contact
    findings. v1 cannot honestly sort them into Cat1 vs Cat2 without reading the narrative, so it
    takes the faster page: a wrong Cat1 costs an unnecessary page, a wrong Cat2 costs an hour."""
    result = classify(CRITICAL, {})
    assert result.category is ACRCategory.CAT1
    assert result.is_critical
    assert result.ack_minutes == 60
    assert result.finding == "tension pneumothorax"


def test_several_flags_are_all_named_in_the_finding():
    result = classify(
        {"criticalFlags": [{"label": "pneumothorax", "severity": "critical"},
                           {"label": "aortic dissection", "severity": "critical"}]}, {})
    assert result.finding == "pneumothorax, aortic dissection"


def test_a_failed_verification_is_cat2_with_a_24_hour_clock():
    """Something is wrong with the REPORT (an uncommunicated critical, a missing section). A human
    must look — but that is not the same as a confirmed life-threatening finding."""
    result = classify({}, {"verificationStatus": "FAIL", "requiresHumanReview": True,
                           "issues": [{"message": "critical finding not communicated"}]})
    assert result.category is ACRCategory.CAT2
    assert result.is_critical
    assert result.ack_minutes == 1440
    assert result.finding == "critical finding not communicated"


def test_a_clean_study_is_not_critical_and_gets_no_clock():
    """No ack clock on a normal study — that is how alert fatigue starts."""
    result = classify({"criticalFlags": []}, {"verificationStatus": "PASS"})
    assert result.category is ACRCategory.NONE
    assert not result.is_critical
    assert result.ack_minutes is None


def test_a_critical_flag_outranks_a_passing_verification():
    """Verification can PASS while the impression still flagged a critical finding (the finding was
    properly communicated). It is still a critical result."""
    result = classify(CRITICAL, {"verificationStatus": "PASS"})
    assert result.category is ACRCategory.CAT1


def test_ack_windows_are_deployment_overridable(monkeypatch):
    """A tertiary centre's Cat1 window is not a rural clinic's."""
    monkeypatch.setenv("CRITCOM_CAT1_ACK_TIMEOUT_MINUTES", "15")
    monkeypatch.setenv("CRITCOM_CAT2_ACK_TIMEOUT_MINUTES", "240")
    assert classify(CRITICAL, {}).ack_minutes == 15
    assert classify({}, {"verificationStatus": "FAIL"}).ack_minutes == 240
