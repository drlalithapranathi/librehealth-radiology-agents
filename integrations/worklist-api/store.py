"""Durable priority store for the Worklist API (M2 issue #20).

The orchestrator is the source of truth for reading priority — see the CLAUDE.md
locked decision "Worklist priority source of truth = orchestrator state". Once
the triage agent produces a `priorityScore` for a study, the workflow's
`publish_priority_activity` (orchestrator/activities.py) publishes it here, and
the Worklist API's `GET /worklist` reads back from this store to sort the OHIF
reading list.

Design mirrors `orchestrator/ingress_store.py`:
  * SQLite (stdlib, no new dependency)
  * WAL + synchronous=FULL so a committed write survives a Worklist API restart
  * single-process access is sufficient today; swap for Redis/Postgres in M3
    when the API scales horizontally — callers depend only on the method surface

No PHI: only IDs + tier + score. The full StudyContext lives in Temporal; this
store answers "what tier/score for this Study Instance UID?" — everything else
about the study comes from Orthanc at read time.

Owner: Parvati.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS study_priority (
    study_instance_uid TEXT PRIMARY KEY,
    workflow_id        TEXT NOT NULL,
    tier               TEXT NOT NULL,
    score              INTEGER NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_study_priority_wf ON study_priority (workflow_id);
"""


class PriorityStore:
    """The Worklist API's local priority index."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def put(self, study_instance_uid: str, workflow_id: str,
            tier: str, score: int, updated_at: str) -> None:
        """Upsert (a re-fired triage would carry the current score, which wins)."""
        self._db.execute(
            "INSERT INTO study_priority "
            "  (study_instance_uid, workflow_id, tier, score, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(study_instance_uid) DO UPDATE SET "
            "  workflow_id = excluded.workflow_id, "
            "  tier        = excluded.tier, "
            "  score       = excluded.score, "
            "  updated_at  = excluded.updated_at",
            (study_instance_uid, workflow_id, tier, score, updated_at),
        )
        self._db.commit()

    def get(self, study_instance_uid: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT study_instance_uid, workflow_id, tier, score, updated_at "
            "FROM study_priority WHERE study_instance_uid = ?",
            (study_instance_uid,),
        ).fetchone()
        if not row:
            return None
        return {"studyInstanceUID": row[0], "workflowId": row[1],
                "priorityTier": row[2], "priorityScore": row[3],
                "updatedAt": row[4]}

    def all(self) -> dict[str, dict]:
        """Map from StudyInstanceUID to priority record. Used by the join in
        `/worklist` — single query, no N+1."""
        rows = self._db.execute(
            "SELECT study_instance_uid, workflow_id, tier, score, updated_at "
            "FROM study_priority").fetchall()
        return {r[0]: {"studyInstanceUID": r[0], "workflowId": r[1],
                       "priorityTier": r[2], "priorityScore": r[3],
                       "updatedAt": r[4]} for r in rows}

    def size(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM study_priority").fetchone()[0]

    def close(self) -> None:
        self._db.close()
