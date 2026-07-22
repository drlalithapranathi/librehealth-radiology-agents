"""Unit tests for the demo referring-physician roster + assignment (#76, build item 1)."""
import sys
import pathlib

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import referrers as R  # noqa: E402


def test_roster_entries_are_well_formed():
    assert R.REFERRERS, "the demo needs at least one referring physician"
    usernames = [x["username"] for x in R.REFERRERS]
    assert len(usernames) == len(set(usernames)), "usernames must be unique (they key idempotency)"
    for x in R.REFERRERS:
        assert x["username"] and x["given"] and x["family"]
        assert x["gender"] in ("M", "F", "U")


def test_assign_is_deterministic():
    assert R.assign("19000001") == R.assign("19000001")


def test_assign_is_stable_per_subject_regardless_of_p_prefix():
    # 'p10000032' and '10000032' are the same MIMIC subject (the fetch tool treats `p` as optional),
    # so they must resolve to the same ordering physician.
    assert R.assign("p10000032") == R.assign("10000032")


def test_assign_returns_a_roster_member():
    for subject in ("19000001", "19000002", "19000003", "p10000032", "42"):
        assert R.assign(subject) in R.REFERRERS


def test_assign_spreads_across_the_roster():
    # consecutive subjects walk the roster, so the demo shows more than one ordering provider
    picks = {R.assign(str(19000000 + i))["username"] for i in range(len(R.REFERRERS))}
    assert len(picks) == len(R.REFERRERS)


def test_assign_handles_a_digitless_subject_without_raising():
    got = R.assign("abc")  # no digits -> character-sum bucket, still deterministic
    assert got in R.REFERRERS
    assert R.assign("abc") == got


def test_assign_honours_a_custom_roster():
    roster = [{"username": "solo", "given": "One", "family": "Only", "gender": "U"}]
    assert R.assign("anything", roster=roster)["username"] == "solo"


def test_empty_roster_raises():
    with pytest.raises(ValueError):
        R.assign("19000001", roster=[])
