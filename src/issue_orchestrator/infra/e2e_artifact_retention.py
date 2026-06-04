"""Filesystem retention helpers for E2E worktree artifacts."""

from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from .e2e_paths import run_report_artifact_dir

logger = logging.getLogger(__name__)


def delete_run_report_artifacts(worktree_path: Path, run_id: int) -> None:
    """Delete run-scoped report artifacts from the E2E worktree."""
    run_dir = run_report_artifact_dir(worktree_path, run_id)
    if not run_dir.exists():
        return
    try:
        shutil.rmtree(run_dir)
    except OSError:
        logger.debug("Could not delete E2E report artifacts for run %d", run_id)


def prune_worktree_artifacts(worktree_path: Path, cutoff: str) -> None:
    """Clean old session dirs and timeline events from the E2E worktree."""
    _prune_worktree_timeline(worktree_path, cutoff)
    _prune_worktree_sessions(worktree_path, cutoff)


def _prune_worktree_timeline(worktree_path: Path, cutoff: str) -> None:
    wt_timeline = worktree_path / ".issue-orchestrator" / "state" / "timeline.sqlite"
    if not wt_timeline.exists():
        return
    try:
        with sqlite3.connect(wt_timeline) as conn:
            conn.execute("DELETE FROM timeline_events WHERE timestamp < ?", (cutoff,))
    except Exception:
        logger.debug("Could not prune worktree timeline", exc_info=True)


def _prune_worktree_sessions(worktree_path: Path, cutoff: str) -> None:
    sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
    if not sessions_dir.is_dir():
        return
    try:
        cutoff_ts = datetime.fromisoformat(cutoff).timestamp()
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
