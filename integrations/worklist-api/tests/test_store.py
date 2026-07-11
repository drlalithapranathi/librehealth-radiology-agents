"""Priority store unit tests. In-memory DB per test (no fixture teardown needed)."""
from __future__ import annotations

from store import PriorityStore


def _fresh() -> PriorityStore:
    return PriorityStore(":memory:")


def test_put_then_get_round_trip():
    s = _fresh()
    s.put("1.2.3", "wf_1", "STAT", 95, "2026-07-10T00:00:00Z")
    got = s.get("1.2.3")
    assert got == {"studyInstanceUID": "1.2.3", "workflowId": "wf_1",
                   "priorityTier": "STAT", "priorityScore": 95,
                   "updatedAt": "2026-07-10T00:00:00Z"}


def test_get_unknown_returns_none():
    assert _fresh().get("does-not-exist") is None


def test_put_is_idempotent_upsert():
    """A re-fired triage (retry, workflow re-run) must not duplicate rows —
    same studyInstanceUID upserts the current tier/score."""
    s = _fresh()
    s.put("1.2.3", "wf_1", "ROUTINE", 50, "2026-07-10T00:00:00Z")
    s.put("1.2.3", "wf_1", "URGENT",  72, "2026-07-10T01:00:00Z")
    assert s.size() == 1
    assert s.get("1.2.3")["priorityTier"] == "URGENT"
    assert s.get("1.2.3")["priorityScore"] == 72


def test_all_returns_dict_keyed_by_uid():
    """`all()` is the single-query join used by /worklist — must return every
    study, keyed for O(1) lookup."""
    s = _fresh()
    s.put("uid1", "wf_1", "STAT",    95, "t1")
    s.put("uid2", "wf_2", "URGENT",  70, "t2")
    s.put("uid3", "wf_3", "ROUTINE", 40, "t3")
    got = s.all()
    assert set(got.keys()) == {"uid1", "uid2", "uid3"}
    assert got["uid1"]["priorityTier"] == "STAT"
    assert got["uid2"]["priorityScore"] == 70
    assert got["uid3"]["workflowId"] == "wf_3"


def test_size_reflects_upserts():
    s = _fresh()
    assert s.size() == 0
    s.put("uid1", "wf", "STAT", 90, "t")
    assert s.size() == 1
    s.put("uid1", "wf", "URGENT", 70, "t")  # upsert same UID
    assert s.size() == 1
    s.put("uid2", "wf", "ROUTINE", 50, "t")
    assert s.size() == 2


def test_durability_across_reopen(tmp_path):
    """A committed write must survive a Worklist API restart. Not testable
    with :memory:, so we use a tmp_path DB and reopen it."""
    path = str(tmp_path / "priority.sqlite")
    s = PriorityStore(path)
    s.put("uid1", "wf_1", "STAT", 90, "t")
    s.close()

    s2 = PriorityStore(path)
    assert s2.get("uid1") == {"studyInstanceUID": "uid1", "workflowId": "wf_1",
                              "priorityTier": "STAT", "priorityScore": 90,
                              "updatedAt": "t"}
