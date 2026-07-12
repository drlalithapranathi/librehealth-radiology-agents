"""#54: a collapsed escalation ladder must be OPERATOR-VISIBLE, not just a log line.

The sign-off gate's soft fallback (one tier timeout, one flat page) is the right safety call and
stays — but on its own it is silent: a broken policy deploy shrinks the whole ladder to a single
page and the system looks healthy. These pin the loud half: the dead letter lands on the same
/admin/dead-letters surface the poller already uses, and the two kinds stay tellable apart.

The workflow-side behavior (the gate still pages; a failed alert never costs the page) lives in
test_escalation_ladder.py.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

pytest.importorskip("temporalio", reason="orchestrator deps not installed")
pytest.importorskip("fastapi", reason="orchestrator deps not installed")

from temporalio.testing import ActivityEnvironment  # noqa: E402

import orchestrator.activities as activities  # noqa: E402
from orchestrator.ingress_store import (  # noqa: E402
    KIND_POLICY_LOAD_FAILURE,
    KIND_SIGNOFF_DROP,
    IngressStore,
)


def test_policy_load_failure_is_recorded_as_a_dead_letter():
    store = IngressStore(":memory:")
    store.add_policy_load_failure("wf_1", "STAT", 3, "escalation policy could not be loaded",
                                  "2026-07-12T00:00:00Z")
    letters = store.dead_letters()
    store.close()

    assert len(letters) == 1
    dl = letters[0]
    assert dl["kind"] == KIND_POLICY_LOAD_FAILURE
    assert dl["workflowId"] == "wf_1"
    assert dl["attempts"] == 3
    assert "STAT" in dl["reason"]          # the tier that lost its ladder is on the row
    assert "policy" in dl["reason"]


def test_one_row_per_workflow_however_often_the_gate_re_enters():
    """The verify loop can re-enter the gate many times for one study; a broken policy would then
    fire the alert on every entry. Operators need a signal, not a flood."""
    store = IngressStore(":memory:")
    for _ in range(5):
        store.add_policy_load_failure("wf_1", "ROUTINE", 3, "escalation policy could not be loaded",
                                      "2026-07-12T00:00:00Z")
    letters = store.dead_letters()
    store.close()
    assert len(letters) == 1


def test_the_two_kinds_stay_tellable_apart_on_one_surface():
    """#54 reuses the #29 dead-letter surface rather than inventing a second one — so `kind` is
    what tells an operator "reconcile a lost sign-off" from "fix the escalation policy"."""
    store = IngressStore(":memory:")
    store.add_dead_letter("DiagnosticReport/7", "wf_1", 4, "workflow evicted", "2026-07-12T00:00:01Z")
    store.add_policy_load_failure("wf_2", "URGENT", 3, "escalation policy could not be loaded",
                                  "2026-07-12T00:00:02Z")
    letters = store.dead_letters()
    store.close()

    by_kind = {dl["kind"]: dl for dl in letters}
    assert set(by_kind) == {KIND_SIGNOFF_DROP, KIND_POLICY_LOAD_FAILURE}
    # The sign-off row is untouched by #54 — same reportId, and it defaults to the pre-#54 kind.
    assert by_kind[KIND_SIGNOFF_DROP]["reportId"] == "DiagnosticReport/7"


def test_a_store_from_before_54_is_migrated_not_broken(tmp_path):
    """A deployed store on a mounted volume has a `dead_letters` with NO `kind` column, and
    CREATE TABLE IF NOT EXISTS will not add one — so without the migration every dead-letter write
    (including the #29 sign-off path that already worked) would fail on the next release."""
    db = str(tmp_path / "old.sqlite")
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE dead_letters (
            report_id   TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            attempts    INTEGER NOT NULL,
            reason      TEXT NOT NULL,
            dropped_at  TEXT NOT NULL
        );
        INSERT INTO dead_letters VALUES ('DiagnosticReport/1', 'wf_old', 4, 'workflow evicted',
                                         '2026-07-01T00:00:00Z');
    """)
    old.commit()
    old.close()

    store = IngressStore(db)          # <- migrates
    letters = store.dead_letters()
    # The pre-existing row survives, and is backfilled with its true (pre-#54) meaning.
    assert len(letters) == 1
    assert letters[0]["reportId"] == "DiagnosticReport/1"
    assert letters[0]["kind"] == KIND_SIGNOFF_DROP
    # ...and the new write path works against the migrated table.
    store.add_policy_load_failure("wf_new", "STAT", 3, "escalation policy could not be loaded",
                                  "2026-07-12T00:00:00Z")
    assert {dl["kind"] for dl in store.dead_letters()} == {KIND_SIGNOFF_DROP,
                                                           KIND_POLICY_LOAD_FAILURE}
    store.close()


def test_activity_writes_to_the_store_the_ingress_endpoint_reads(tmp_path, monkeypatch):
    """The worker writes the dead letter, the ingress serves it — two processes, one sqlite file.
    If they ever resolve DIFFERENT paths the alert is written where nobody looks, so pin that they
    agree."""
    db = str(tmp_path / "ingress_state.db")
    monkeypatch.setenv("INGRESS_STORE_PATH", db)

    asyncio.run(ActivityEnvironment().run(
        activities.record_policy_failure_activity,
        "wf_42", "STAT", "escalation policy could not be loaded", 3,
    ))

    # Read it back the way /admin/dead-letters does — through the ingress's own path resolution.
    import orchestrator.ingress as ingress
    assert ingress._default_store_path() == db
    store = IngressStore(ingress._default_store_path())
    letters = store.dead_letters()
    store.close()

    assert [dl["kind"] for dl in letters] == [KIND_POLICY_LOAD_FAILURE]
    assert letters[0]["workflowId"] == "wf_42"
