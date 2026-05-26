"""Run artifact resolution helpers for active session lifecycle paths."""

from __future__ import annotations

import logging
from pathlib import Path

from ..domain.models import Session
from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)


def resolve_session_run_dir(
    session_output: SessionOutput,
    session: Session,
) -> Path | None:
    """Resolve the run directory for a session using artifact identity.

    Newly launched sessions carry the exact artifact directory recorded at
    launch. That path is authoritative and provider-agnostic: terminal adapter
    names, phase names, and provider-specific artifact layouts must not be
    re-derived during timeout/completion handling.

    Legacy/restored sessions can lack the launch-time pointer. Only those
    sessions use discovery as a recovery path.
    """
    if session.run_dir is not None:
        if not session.run_dir.exists():
            logger.warning(
                "[%s] Session run_dir is recorded but missing: %s",
                session.terminal_id,
                session.run_dir,
            )
        return session.run_dir

    run_dir = session_output.find_run_dir(
        session.worktree_path,
        session.terminal_id,
    )
    if isinstance(run_dir, Path):
        return run_dir

    fallback = session_output.find_run_dir(session.worktree_path)
    if not isinstance(fallback, Path):
        return None

    manifest = session_output.read_manifest(fallback) or {}
    if manifest.get("issue_number") == session.issue.number:
        return fallback
    return None
