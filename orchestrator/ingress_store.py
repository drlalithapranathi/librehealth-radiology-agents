"""Durable ingress state (issue #6).

The RIS sign-off poller lives in the ingress process, but the radiologist human-gate lasts
hours-to-days. Two pieces of poller state must therefore survive an ingress restart
(deploy / crash / OOM) during that wait, or the sign-off is silently lost:

  * the report->workflow join index — populated when a study starts; without it a finalized
    report matches nothing and the durably-waiting workflow is stranded forever;
  * the poll cursor (+ the ids already signalled at its boundary) — without it the cursor
    resets to "now" on restart and every report finalized during the downtime is skipped.

Temporal preserves the *workflow*, but not the *ingress poller that delivers the signal* — so
Temporal-side restart tests pass green while the sign-off never arrives. This store closes that
gap. It is deliberately SQLite (stdlib, no new dependency): durable across a process restart and
matched to the current single-ingress-process design. Swap the backend for Redis/Postgres in M2
when the ingress scales horizontally — callers only depend on the method surface below.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_index (
    join_key    TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_workflow_index_wf ON workflow_index (workflow_id);
CREATE TABLE IF NOT EXISTS poller_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    cursor        TEXT,
    signalled_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS failed_signals (
    report_id    TEXT PRIMARY KEY,
    workflow_id  TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_attempt TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS dead_letters (
    report_id   TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    attempts    INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    dropped_at  TEXT NOT NULL
);
"""


class IngressStore:
    """Durable home for the report->workflow index and the poll cursor.

    Access is from the single ingress event loop (webhook writer + poller reader), so one
    connection is sufficient; `check_same_thread=False` is a safety net for test drivers.
    WAL + synchronous=FULL make committed writes survive a process/host crash.
    """

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ---- report -> workflow join index -------------------------------------------
    def put_index(self, join_key: str, workflow_id: str) -> None:
        self._db.execute(
            "INSERT INTO workflow_index (join_key, workflow_id) VALUES (?, ?) "
            "ON CONFLICT(join_key) DO UPDATE SET workflow_id = excluded.workflow_id",
            (join_key, workflow_id),
        )
        self._db.commit()

    def workflow_id_for(self, join_key: str) -> Optional[str]:
        row = self._db.execute(
            "SELECT workflow_id FROM workflow_index WHERE join_key = ?", (join_key,)
        ).fetchone()
        return row[0] if row else None

    def evict_workflow(self, workflow_id: str) -> None:
        """Drop every join key for a workflow. Called by the ingress reconciliation once the
        workflow has completed/terminated, so the index stays bounded to studies still awaiting
        sign-off while an addendum can still route as long as the workflow is running."""
        self._db.execute("DELETE FROM workflow_index WHERE workflow_id = ?", (workflow_id,))
        self._db.commit()

    def index_size(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM workflow_index").fetchone()[0]

    def indexed_workflow_ids(self) -> list[str]:
        """Distinct workflow ids currently in the index (for completion-based reconciliation)."""
        return [r[0] for r in self._db.execute(
            "SELECT DISTINCT workflow_id FROM workflow_index").fetchall()]

    # ---- poll cursor (+ boundary dedup set) --------------------------------------
    def load_cursor(self) -> tuple[Optional[str], set[str]]:
        """Return (cursor, signalled-ids-at-boundary). (None, set()) on a fresh store."""
        row = self._db.execute(
            "SELECT cursor, signalled_ids FROM poller_state WHERE id = 1"
        ).fetchone()
        if not row:
            return None, set()
        return row[0], set(json.loads(row[1] or "[]"))

    def save_cursor(self, cursor: str, signalled_ids: set[str]) -> None:
        self._db.execute(
            "INSERT INTO poller_state (id, cursor, signalled_ids) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cursor = excluded.cursor, "
            "signalled_ids = excluded.signalled_ids",
            (cursor, json.dumps(sorted(signalled_ids))),
        )
        self._db.commit()

    # ---- failed-signal tracking + dead letters (#29) ------------------------------
    # A signed report the poller could not deliver is retried for free by the held cursor.
    # The retry ends when reconciliation evicts the target workflow's index rows; at that point
    # the report re-enters as unmapped and would be silently dropped. Tracking failures durably
    # lets the poller tell "was ours, workflow gone" apart from "never ours" — the former is a
    # dead letter a human must see, the latter is routine fhir2 noise. IDs only, never PHI.

    def record_failed_signal(self, report_id: str, workflow_id: str, at_iso: str) -> None:
        """Upsert one failed delivery attempt for a mapped report (attempts increment)."""
        self._db.execute(
            "INSERT INTO failed_signals (report_id, workflow_id, first_seen, last_attempt) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(report_id) DO UPDATE SET workflow_id = excluded.workflow_id, "
            "last_attempt = excluded.last_attempt, attempts = attempts + 1",
            (report_id, workflow_id, at_iso, at_iso),
        )
        self._db.commit()

    def clear_failed_signal(self, report_id: str) -> None:
        """A later attempt delivered the report: the failure record has served its purpose."""
        self._db.execute("DELETE FROM failed_signals WHERE report_id = ?", (report_id,))
        self._db.commit()

    def failed_signal_for(self, report_id: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT report_id, workflow_id, first_seen, last_attempt, attempts "
            "FROM failed_signals WHERE report_id = ?", (report_id,)
        ).fetchone()
        if not row:
            return None
        return {"reportId": row[0], "workflowId": row[1], "firstSeen": row[2],
                "lastAttempt": row[3], "attempts": row[4]}

    def add_dead_letter(self, report_id: str, workflow_id: str, attempts: int,
                        reason: str, at_iso: str) -> None:
        """Record a permanently dropped sign-off. Idempotent on report_id (a re-scan at the same
        cursor boundary must not duplicate the row)."""
        self._db.execute(
            "INSERT INTO dead_letters (report_id, workflow_id, attempts, reason, dropped_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(report_id) DO NOTHING",
            (report_id, workflow_id, attempts, reason, at_iso),
        )
        self._db.commit()

    def dead_letters(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT report_id, workflow_id, attempts, reason, dropped_at "
            "FROM dead_letters ORDER BY dropped_at").fetchall()
        return [{"reportId": r[0], "workflowId": r[1], "attempts": r[2],
                 "reason": r[3], "droppedAt": r[4]} for r in rows]

    def close(self) -> None:
        self._db.close()
