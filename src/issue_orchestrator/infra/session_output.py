"""Session output paths for per-session logs and artifacts."""

from __future__ import annotations

from pathlib import Path

SESSION_OUTPUT_DIR = "sessions"
SESSION_LOG_NAME = "session.log"
PANE_LOG_NAME = "pane.log"
WORKTREE_NOTE_NAME = "worktree.json"


def session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Return the per-session output directory for a worktree."""
    return worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR / session_name


def ensure_session_output_dir(worktree_path: Path, session_name: str) -> Path:
    """Create and return the per-session output directory."""
    output_dir = session_output_dir(worktree_path, session_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def find_session_log_path(worktree_path: Path, session_name: str) -> Path | None:
    """Find the local session log for a session, if present."""
    output_dir = session_output_dir(worktree_path, session_name)
    for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
        candidate = output_dir / filename
        if candidate.exists():
            return candidate
    return None


def find_latest_session_log_path(worktree_path: Path) -> Path | None:
    """Find the most recently updated local session log in a worktree."""
    base_dir = worktree_path / ".issue-orchestrator" / SESSION_OUTPUT_DIR
    if not base_dir.exists():
        return None
    log_candidates: list[Path] = []
    for session_dir in base_dir.iterdir():
        if not session_dir.is_dir():
            continue
        for filename in (SESSION_LOG_NAME, PANE_LOG_NAME):
            candidate = session_dir / filename
            if candidate.exists():
                log_candidates.append(candidate)
    if not log_candidates:
        return None
    return max(log_candidates, key=lambda path: path.stat().st_mtime)
