"""E2E run history pruning and reset support."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.timeline_store import TimelineStore

logger = logging.getLogger(__name__)

ConnectFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


class E2EHistoryMaintenance:
    """Owns E2E run-history deletion and artifact cleanup."""

    def __init__(self, connect: ConnectFactory) -> None:
        self._connect = connect

    def prune_old_runs(
        self,
        retention_count: int,
        timeline_store: "TimelineStore | None" = None,
        e2e_worktree_path: "Path | None" = None,
    ) -> int:
        """Delete runs beyond the retention count, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, log_path FROM e2e_runs
                WHERE id NOT IN (
                    SELECT id FROM e2e_runs
                    ORDER BY started_at DESC
                    LIMIT ?
                )
                ORDER BY started_at ASC
                """,
                (retention_count,),
            ).fetchall()

            if not rows:
                return 0

            pruned = 0
            for row in rows:
                run_id = row["id"]
                log_path = row["log_path"]
                self._delete_run_rows(conn, run_id)
                self._delete_log_file(log_path)
                self._delete_timeline_events(timeline_store, run_id)
                pruned += 1

            if pruned and e2e_worktree_path is not None:
                self._prune_worktree_artifacts(e2e_worktree_path, retention_count)

            if pruned:
                logger.info("Pruned %d old E2E run(s) (retention=%d)", pruned, retention_count)
            return pruned

    def reset_all_history(
        self,
        timeline_store: "TimelineStore | None" = None,
    ) -> dict[str, int]:
        """Delete all E2E run history. Returns counts of deleted items."""
        counts: dict[str, int] = {}
        with self._connect() as conn:
            runs = conn.execute("SELECT id, log_path FROM e2e_runs").fetchall()

            for table in ("e2e_test_results", "e2e_failure_issues", "e2e_run_issues", "e2e_flake_history", "e2e_runs"):
                cursor = conn.execute(f"DELETE FROM {table}")  # noqa: S608 — table names are hardcoded literals
                counts[table] = cursor.rowcount

            log_count = 0
            for row in runs:
                if row["log_path"]:
                    if self._delete_log_file(row["log_path"]):
                        log_count += 1
                self._delete_timeline_events(timeline_store, row["id"])

            counts["log_files"] = log_count

        logger.info("Reset E2E history: %s", counts)
        return counts

    def _delete_run_rows(self, conn: sqlite3.Connection, run_id: int) -> None:
        conn.execute("DELETE FROM e2e_test_results WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_failure_issues WHERE first_failing_run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_run_issues WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_flake_history WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM e2e_runs WHERE id = ?", (run_id,))

    @staticmethod
    def _delete_log_file(log_path: str | None) -> bool:
        if not log_path:
            return False
        try:
            Path(log_path).unlink(missing_ok=True)
            return True
        except OSError:
            return False

    @staticmethod
    def _delete_timeline_events(timeline_store: "TimelineStore | None", run_id: int) -> None:
        if timeline_store is None:
            return
        try:
            from ..domain.timeline_key import TimelineKey

            store_key = TimelineKey.for_e2e_run(run_id).to_store_key()
            timeline_store.delete(store_key)
        except Exception:
            logger.debug("Could not delete timeline for E2E run %d", run_id)

    def _prune_worktree_artifacts(self, worktree_path: Path, retention_count: int) -> None:
        """Clean old session dirs and timeline events from the E2E worktree."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT started_at FROM e2e_runs
                ORDER BY started_at DESC
                LIMIT 1 OFFSET ?
                """,
                (max(0, retention_count - 1),),
            ).fetchone()

        if not row:
            return
        cutoff = row["started_at"]
        self._prune_worktree_timeline(worktree_path, cutoff)
        self._prune_worktree_sessions(worktree_path, cutoff)

    @staticmethod
    def _prune_worktree_timeline(worktree_path: Path, cutoff: str) -> None:
        wt_timeline = worktree_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
        if not wt_timeline.exists():
            return
        conn = None
        try:
            uri = f"file:{wt_timeline}"
            conn = sqlite3.connect(uri)
            conn.execute(
                "DELETE FROM timeline_events WHERE timestamp < ?",
                (cutoff,),
            )
            conn.commit()
        except Exception:
            logger.debug("Could not prune worktree timeline", exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _prune_worktree_sessions(worktree_path: Path, cutoff: str) -> None:
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        if not sessions_dir.is_dir():
            return
        try:
            cutoff_dt = datetime.fromisoformat(cutoff)
            cutoff_ts = cutoff_dt.timestamp()
        except (ValueError, TypeError):
            return

        for entry in sessions_dir.iterdir():
            if not entry.is_dir():
                continue
            try:
                if entry.stat().st_mtime < cutoff_ts:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                pass
