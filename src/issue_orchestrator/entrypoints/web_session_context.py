from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator


@dataclass(frozen=True)
class IssueSessionContext:
    """Resolved latest session context for an issue across active/history/storage."""

    worktree_path: Path | None = None
    session_name: str | None = None
    run_dir: Path | None = None


_get_orchestrator: Callable[[], Orchestrator | None] | None = None


def configure_web_session_context(*, get_orchestrator: Callable[[], Orchestrator | None]) -> None:
    """Configure access to the web module's orchestrator singleton."""
    global _get_orchestrator
    _get_orchestrator = get_orchestrator


def get_web_orchestrator() -> Orchestrator | None:
    """Return the configured web orchestrator, if available."""
    if _get_orchestrator is None:
        return None
    return _get_orchestrator()


def _latest_session_history_entry(issue_number: int) -> Any | None:
    """Return the most recent history entry for an issue."""
    orchestrator = get_web_orchestrator()
    if not orchestrator:
        return None
    for entry in reversed(orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry
    return None


def resolve_issue_session_context(issue_number: int) -> IssueSessionContext:
    """Resolve current issue session context from active or local history."""
    orchestrator = get_web_orchestrator()
    if not orchestrator:
        return IssueSessionContext()

    from ..execution.session_output_adapter import FileSystemSessionOutput

    session_output = FileSystemSessionOutput()

    for session in orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            run_dir = session_output.find_run_dir(
                session.worktree_path,
                session_name=session.terminal_id,
            )
            return IssueSessionContext(
                worktree_path=session.worktree_path,
                session_name=session.terminal_id,
                run_dir=run_dir,
            )

    history_entry = _latest_session_history_entry(issue_number)
    if history_entry:
        worktree_value = getattr(history_entry, "worktree_path", None)
        worktree_path = Path(worktree_value) if worktree_value else None
        run_dir = None
        if worktree_path:
            run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
        session_name = session_output.session_name_from_path(str(run_dir)) if run_dir else None
        return IssueSessionContext(
            worktree_path=worktree_path,
            session_name=session_name,
            run_dir=run_dir,
        )

    # Fail-fast: do not scan sibling worktrees/repos for session state.
    return IssueSessionContext()


def worktree_path_from_run_dir(run_dir: Path) -> Path | None:
    """Infer worktree root from a run directory path."""
    parts = run_dir.resolve().parts
    if ".issue-orchestrator" not in parts:
        return None
    idx = parts.index(".issue-orchestrator")
    if idx <= 0:
        return None
    return Path(*parts[:idx])


def issue_title_for(issue_number: int) -> str:
    """Resolve the best-known title for an issue from active and persisted state."""
    orchestrator = get_web_orchestrator()
    if not orchestrator:
        return f"Issue #{issue_number}"
    for session in orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            return session.issue.title
    for issue in orchestrator.state.cached_queue_issues:
        if issue.number == issue_number:
            return issue.title
    for entry in reversed(orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry.title
    return f"Issue #{issue_number}"
