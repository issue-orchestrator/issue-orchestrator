"""Run artifact resolution helpers for active session lifecycle paths."""

from __future__ import annotations

from collections.abc import Sequence
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
    return resolve_run_dir(
        session_output,
        worktree_path=session.worktree_path,
        session_name=session.terminal_id,
        issue_number=session.issue.number,
        recorded_run_dir=session.run_dir,
        completion_path=session.completion_path,
    )


def resolve_run_dir(
    session_output: SessionOutput,
    *,
    worktree_path: Path,
    session_name: str,
    issue_number: int | None,
    recorded_run_dir: Path | None = None,
    completion_path: str | None = None,
    alternate_session_names: Sequence[str] = (),
) -> Path | None:
    """Resolve a session run directory from launch identity or legacy artifacts.

    ``recorded_run_dir`` is authoritative even when the directory is no longer
    present. A missing recorded path means the run artifacts were pruned or
    moved; falling back to discovery could attach diagnostics to a different
    run. Legacy sessions without that pointer use bounded discovery.
    """
    if recorded_run_dir is not None:
        if not recorded_run_dir.exists():
            logger.warning(
                "[%s] Session run_dir is recorded but missing: %s",
                session_name,
                recorded_run_dir,
            )
        return recorded_run_dir

    completion_session_name = _session_name_from_completion_path(
        session_output,
        completion_path,
    )
    session_names = [
        name
        for name in (
            completion_session_name,
            session_name,
            *alternate_session_names,
        )
        if name
    ]
    seen: set[str] = set()
    for candidate_session_name in session_names:
        if candidate_session_name in seen:
            continue
        seen.add(candidate_session_name)
        run_dir = session_output.find_run_dir(
            worktree_path,
            candidate_session_name,
        )
        if isinstance(run_dir, Path):
            return run_dir

    fallback = session_output.find_run_dir(worktree_path)
    if not isinstance(fallback, Path):
        return None

    manifest = session_output.read_manifest(fallback) or {}
    if issue_number is not None and manifest.get("issue_number") == issue_number:
        return fallback
    return None


def _session_name_from_completion_path(
    session_output: SessionOutput,
    completion_path: str | None,
) -> str | None:
    session_name = session_output.session_name_from_path(completion_path)
    return session_name if isinstance(session_name, str) and session_name else None
