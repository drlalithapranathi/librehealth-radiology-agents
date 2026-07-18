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

from radagent_common import paths

# Dead-letter kinds (the `kind` column). A dead letter is anything an operator must see and
# reconcile by hand; the kind says which surface degraded.
KIND_SIGNOFF_DROP = "signoff-drop"                          # a sign-off the poller gave up on (#29)
KIND_POST_ARCHIVE_ADDENDUM = "post-archive-addendum"         # a correction whose workflow had already finished (#66)
KIND_POLICY_LOAD_FAILURE = "escalation-policy-load-failure"  # the ladder collapsed to one page (#54)
KIND_SIGNOFF_ABANDONED = "signoff-abandoned"                 # gate ran out of ladder, nobody acked (#57)

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
    dropped_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'signoff-drop'
);
"""


def default_store_path() -> str:
    """The one store path the ingress AND the worker both resolve to.

    Both processes run in the same orchestrator container (compose runs the worker and uvicorn
    side by side), so the sign-off poller's dead letters and the workflow's policy-load dead
    letters (#54) land in the SAME sqlite file and surface on the same /admin/dead-letters.

    Production MUST override INGRESS_STORE_PATH to a durable *mounted volume* — the default is a
    path inside the container and does not survive `docker compose down`.
    """
    return os.environ.get("INGRESS_STORE_PATH") or str(paths.repo_root() / "ingress_state.db")


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
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Additive column migrations for stores created by an earlier release.

        `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so a store on a mounted
        volume from before #54 keeps a `dead_letters` with no `kind` column and every write would
        fail. Backfill it with the pre-#54 meaning (every existing row IS a dropped sign-off).
        """
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(dead_letters)")}
        if "kind" not in columns:
            self._db.execute(
                "ALTER TABLE dead_letters ADD COLUMN kind TEXT NOT NULL "
                f"DEFAULT '{KIND_SIGNOFF_DROP}'"
            )
        # A failure record must remember WHICH signal failed: the eventual dead letter is
        # classified from this history, not from whatever version fhir2 serves at eviction time
        # (a report amended DURING the outage would otherwise re-label a genuinely lost sign-off
        # as an addendum -- the dangerous direction). Pre-migration rows default to 'final':
        # classifying an unknown history as a possible lost sign-off is the safe error.
        fs_columns = {row[1] for row in self._db.execute("PRAGMA table_info(failed_signals)")}
        if "signal" not in fs_columns:
            self._db.execute(
                "ALTER TABLE failed_signals ADD COLUMN signal TEXT NOT NULL DEFAULT 'final'"
            )

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
        """Return (cursor, signalled-ids-at-boundary). (None, set()) on a fresh store.

        Upgrade shim: a store written before the version-keyed dedup (#66) holds BARE report
        ids — rewrite them as id@cursor on load. For a store saved with an un-held cursor (the
        quiet steady state) that key is exact: the pre-#66 prune kept only the boundary, whose
        stamp IS the cursor. Without the rewrite, the first post-upgrade poll re-signals the
        boundary report; if its workflow has meanwhile completed, that decays into a per-poll
        "matched no waiting workflow" warning that never ages out on a quiet system, or — if
        the index row is still pending reconciliation — a held cursor and a spurious
        signoff-drop dead letter for a study that finished normally.

        Known residual, kept deliberately: a store saved while the cursor was HELD by failing
        signals (#29) may also hold ids whose true stamp is later than the cursor; those
        migrate to a phantom key and their report re-signals once after the deploy — the
        pre-shim behaviour for exactly those ids. The phantom key can only err toward
        RE-delivery (it matches nothing real, and FHIR search returns one current version per
        resource), never toward swallowing; a shim strong enough to silence that re-signal
        (bare-id fallback matching) could swallow a genuine addendum, the one direction this
        pipeline must never choose. FHIR ids cannot contain '@', so the marker is unambiguous.
        The rewrite persists on the next save_cursor.
        """
        row = self._db.execute(
            "SELECT cursor, signalled_ids FROM poller_state WHERE id = 1"
        ).fetchone()
        if not row:
            return None, set()
        cursor, ids = row[0], set(json.loads(row[1] or "[]"))
        if cursor:
            ids = {i if "@" in i else f"{i}@{cursor}" for i in ids}
        return cursor, ids

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

    def record_failed_signal(self, report_id: str, workflow_id: str, at_iso: str,
                             signal: str = "final") -> None:
        """Upsert one failed delivery attempt for a mapped report (attempts increment).

        `signal` is 'final' or 'addendum' -- which DELIVERY failed. It is 'final'-sticky across
        versions: once a report_finalized delivery has failed, a later retry that happens to
        carry the amended version must not re-label the record, because the thing still
        undelivered is the sign-off."""
        self._db.execute(
            "INSERT INTO failed_signals (report_id, workflow_id, first_seen, last_attempt, signal) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(report_id) DO UPDATE SET workflow_id = excluded.workflow_id, "
            "last_attempt = excluded.last_attempt, attempts = attempts + 1, "
            "signal = CASE WHEN failed_signals.signal = 'final' THEN 'final' "
            "ELSE excluded.signal END",
            (report_id, workflow_id, at_iso, at_iso, signal),
        )
        self._db.commit()

    def clear_failed_signal(self, report_id: str) -> None:
        """A later attempt delivered the report: the failure record has served its purpose."""
        self._db.execute("DELETE FROM failed_signals WHERE report_id = ?", (report_id,))
        self._db.commit()

    def failed_signal_for(self, report_id: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT report_id, workflow_id, first_seen, last_attempt, attempts, signal "
            "FROM failed_signals WHERE report_id = ?", (report_id,)
        ).fetchone()
        if not row:
            return None
        return {"reportId": row[0], "workflowId": row[1], "firstSeen": row[2],
                "lastAttempt": row[3], "attempts": row[4], "signal": row[5]}

    def add_dead_letter(self, report_id: str, workflow_id: str, attempts: int,
                        reason: str, at_iso: str,
                        kind: str = KIND_SIGNOFF_DROP) -> None:
        """Record a permanently dropped sign-off. Idempotent on report_id (a re-scan at the same
        cursor boundary must not duplicate the row)."""
        self._db.execute(
            "INSERT INTO dead_letters (report_id, workflow_id, attempts, reason, dropped_at, kind) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(report_id) DO NOTHING",
            (report_id, workflow_id, attempts, reason, at_iso, kind),
        )
        self._db.commit()

    def add_policy_load_failure(self, workflow_id: str, tier: Optional[str], attempts: int,
                                reason: str, at_iso: str) -> None:
        """Record that a sign-off gate could not load its escalation ladder (#54).

        The gate's soft fallback (one tier timeout, one flat page) is the right safety call and is
        unchanged — but it silently collapses the whole ladder, so a broken policy deploy can page
        a single person once and look healthy forever. This puts it on the same operator surface as
        a dropped sign-off: /admin/dead-letters.

        Keyed per workflow, so a study whose verify loop re-enters the gate records ONE row rather
        than one per entry. The `report_id` column carries a synthetic key here (there is no
        report involved) — read `kind` to tell the two apart.
        """
        self.add_dead_letter(
            report_id=f"{KIND_POLICY_LOAD_FAILURE}:{workflow_id}",
            workflow_id=workflow_id,
            attempts=attempts,
            reason=f"{reason} (tier={tier or 'unknown'})",
            at_iso=at_iso,
            kind=KIND_POLICY_LOAD_FAILURE,
        )

    def add_signoff_abandoned(self, workflow_id: str, tier: Optional[str], pages: int,
                              at_iso: str) -> None:
        """Record a sign-off gate that ran out of ladder with nobody acknowledging (#57).

        This is the LOUD half of removing the silent hold. Before #57 such a study paged its way up
        the ladder, hit the repeat cap, and then waited forever for a signal nothing could send:
        invisible, un-archivable, and -- because it never reached COMMUNICATE -- its critical
        finding never dispatched. Now the gate releases the study and says so here. The report still
        carries a verification FAIL that no human ever acknowledged, so this row is not
        informational: it is a study a human must go and look at.

        Keyed per workflow (one row per study, not per page). `report_id` carries a synthetic key —
        read `kind` to tell the dead-letter surfaces apart.
        """
        self.add_dead_letter(
            report_id=f"{KIND_SIGNOFF_ABANDONED}:{workflow_id}",
            workflow_id=workflow_id,
            attempts=pages,
            reason=(f"sign-off gate exhausted its escalation ladder after {pages} page(s) with no "
                    f"acknowledgement (tier={tier or 'unknown'}); released to COMMUNICATE with the "
                    f"verification FAIL unacknowledged"),
            at_iso=at_iso,
            kind=KIND_SIGNOFF_ABANDONED,
        )

    def dead_letters(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT report_id, workflow_id, attempts, reason, dropped_at, kind "
            "FROM dead_letters ORDER BY dropped_at").fetchall()
        return [{"reportId": r[0], "workflowId": r[1], "attempts": r[2],
                 "reason": r[3], "droppedAt": r[4], "kind": r[5]} for r in rows]

    def close(self) -> None:
        self._db.close()
