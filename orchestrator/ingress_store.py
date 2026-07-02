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

    def close(self) -> None:
        self._db.close()
