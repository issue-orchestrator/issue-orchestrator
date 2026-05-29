"""Fresh lifecycle rerun state lookup for execution services."""

from __future__ import annotations

from pathlib import Path

from ..domain.fresh_lifecycle_rerun import manifest_has_fresh_lifecycle_rerun
from ..ports.session_output import SessionOutput


def parent_has_rerun(
    session_output: SessionOutput,
    worktree: Path,
    parent_session_name: str | None,
) -> bool:
    """Return whether the parent coding session requested a fresh rerun."""
    if not parent_session_name:
        return False
    parent_run_dir = session_output.find_run_dir(worktree, parent_session_name)
    if parent_run_dir is None:
        return False
    return manifest_has_fresh_lifecycle_rerun(session_output.read_manifest(parent_run_dir))
