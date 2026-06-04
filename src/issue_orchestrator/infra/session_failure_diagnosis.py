"""Session failure diagnosis for the web UI.

This module provides diagnostic info for debugging failed sessions,
used by the /api/failure-diagnosis/{issue_number} endpoint.

Separated from ai_diagnose.py to avoid dependency on doctor.py and
execution layer (which would violate architecture contracts).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Config
    from ..ports.session_output import SessionOutput


@dataclass
class SessionFailureDiagnosis:
    """Diagnosis info for a failed session.

    This is returned by the orchestrator to the web layer for the
    /api/failure-diagnosis/{issue_number} endpoint.
    """

    issue_number: int
    ai_system: str
    permission_mode: str
    worktree_path: str | None
    log_path: str | None
    log_exists: bool
    log_context: str | None
    history_status: str | None
    history_reason: str | None
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    review_feedback: list[dict[str, Any]] = field(default_factory=list)
    analysis_headline: str | None = None
    analysis_detail: str | None = None
    analysis_suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {
            "issue_number": self.issue_number,
            "ai_system": self.ai_system,
            "permission_mode": self.permission_mode,
            "worktree_path": self.worktree_path,
            "log_path": self.log_path,
            "log_exists": self.log_exists,
            "log_context": self.log_context,
            "history_status": self.history_status,
            "history_reason": self.history_reason,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "review_feedback": self.review_feedback,
        }
        if self.analysis_headline:
            result["analysis_headline"] = self.analysis_headline
            if self.analysis_detail:
                result["analysis_detail"] = self.analysis_detail
            if self.analysis_suggestions:
                result["analysis_suggestions"] = self.analysis_suggestions
        return result


def _search_worktree_in_base(
    base: Path, repo_name: str, issue_number: int
) -> Path | None:
    """Search for worktree matching issue in a single base directory."""
    # Try direct match first
    candidate = base / f"{repo_name}-{issue_number}"
    if candidate.exists() and candidate.is_dir():
        return candidate

    # Search all worktrees in this base
    for worktree_path in base.glob(f"{repo_name}-*"):
        if not worktree_path.is_dir():
            continue
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        if not sessions_dir.exists():
            continue
        # Look for session dirs matching this issue
        for session_dir in sessions_dir.iterdir():
            if not session_dir.is_dir():
                continue
            name = session_dir.name
            if any(x in name for x in [f"issue-{issue_number}", f"review-{issue_number}", f"rework-{issue_number}"]):
                return worktree_path
    return None


def _find_worktree_for_issue(
    worktree_bases: list[Path],
    repo_name: str,
    issue_number: int,
) -> Path | None:
    """Find the worktree path for an issue."""
    seen: set[Path] = set()

    for base in worktree_bases:
        if not base or not base.exists():
            continue
        base = base.resolve()
        if base in seen:
            continue
        seen.add(base)

        result = _search_worktree_in_base(base, repo_name, issue_number)
        if result:
            return result

    return None


def _find_session_from_history(session_history: list, issue_number: int):
    """Find history entry for issue."""
    for entry in reversed(session_history):
        if entry.issue_number == issue_number:
            return entry
    return None


def _find_worktree_from_active_sessions(active_sessions: list, issue_number: int):
    """Find worktree and agent config from active sessions. Returns (path, config)."""
    for session in active_sessions:
        if session.issue.number == issue_number:
            return session.worktree_path, session.agent_config
    return None, None


def _build_worktree_bases(config: "Config") -> list[Path]:
    """Build list of worktree bases to search."""
    bases: list[Path] = []
    if config.worktree_base:
        bases.append(Path(config.worktree_base))
    for agent in config.agents.values():
        agent_base = getattr(agent, "worktree_base", None)
        if agent_base:
            bases.append(Path(agent_base))
    bases.append(config.repo_root.parent)
    return bases


def _detect_ai_system_and_mode(agent_config, history_entry, agents):
    """Detect AI system and permission mode. Returns (ai_system, permission_mode)."""
    from ..ports.session_log import detect_ai_system_from_command

    if agent_config:
        ai_system = detect_ai_system_from_command(agent_config.command) or "claude-code"
        return ai_system, agent_config.effective_permission_mode

    if history_entry:
        agent_label = history_entry.agent_type
        if agent_label in agents:
            cfg = agents[agent_label]
            ai_system = detect_ai_system_from_command(cfg.command) or "claude-code"
            return ai_system, cfg.effective_permission_mode

    return "claude-code", "unknown"


def _build_warnings_and_suggestions(permission_mode: str, log_path: Path | None, log_context: str | None, worktree_path) -> tuple[list[str], list[str]]:
    """Build warnings and suggestions lists."""
    warnings: list[str] = []
    suggestions: list[str] = []

    if permission_mode == "default":
        warnings.append("permission_mode is 'default' - Claude prompts for permissions in non-interactive mode")
        suggestions.append("Add 'permission_mode: bypassPermissions' to your agent config in YAML")

    if not log_path or not log_path.exists():
        warnings.append("No AI session log found for this issue")
        suggestions.append(f"Check ~/.claude/projects/ for logs related to {worktree_path}")

    if log_context and "permission" in log_context.lower():
        warnings.append("Permission-related errors detected in log")
        suggestions.append("Consider using 'permission_mode: bypassPermissions' for non-interactive sessions")

    return warnings, suggestions


def _load_review_feedback(worktree_path: Path | None) -> list[dict[str, Any]]:
    """Load all review feedback files from a worktree.

    Returns list of dicts with 'cycle', 'path', and 'content' keys.
    """
    if not worktree_path:
        return []

    feedback_dir = worktree_path / ".issue-orchestrator" / "review-feedback"
    if not feedback_dir.exists():
        return []

    result = []
    for path in sorted(feedback_dir.glob("cycle-*.md")):
        try:
            # Extract cycle number from filename (cycle-N.md)
            cycle = int(path.stem.split("-")[1])
            result.append({
                "cycle": cycle,
                "path": str(path),
                "content": path.read_text(),
            })
        except (ValueError, IndexError):
            pass
    return result


def _load_analysis_from_worktree(
    worktree_path: Path | None,
    session_output: "SessionOutput | None" = None,
) -> tuple[str | None, str | None, list[str]]:
    """Try to load analysis.json from the most recent run dir.

    Returns (headline, detail, suggestions).
    """
    if not worktree_path or not session_output:
        return None, None, []

    try:
        from ..control.session_analyzer import load_analysis

        run_dir = session_output.find_run_dir(worktree_path)
        if not run_dir:
            return None, None, []

        analysis = load_analysis(run_dir)
        if not analysis:
            return None, None, []

        return analysis.headline, analysis.detail, list(analysis.suggestions)
    except Exception:
        return None, None, []


def create_session_failure_diagnosis(
    issue_number: int,
    session_history: list,
    active_sessions: list,
    config: "Config",
    agents: dict,
    session_output: "SessionOutput | None" = None,
) -> SessionFailureDiagnosis:
    """Create a failure diagnosis for a session."""
    from ..adapters.session_log.registry import get_log_provider

    history_entry = _find_session_from_history(session_history, issue_number)
    worktree_path, agent_config = _find_worktree_from_active_sessions(active_sessions, issue_number)

    if not worktree_path:
        bases = _build_worktree_bases(config)
        repo_name = config.repo.split("/")[-1] if config.repo else config.repo_root.name
        worktree_path = _find_worktree_for_issue(bases, repo_name, issue_number)

    ai_system, permission_mode = _detect_ai_system_and_mode(agent_config, history_entry, agents)
    provider = get_log_provider(ai_system)

    log_path: Path | None = None
    log_context: str | None = None
    if provider and worktree_path:
        log_path = provider.get_log_path(Path(worktree_path), f"issue-{issue_number}")
        if log_path:
            log_context = provider.get_failure_context(log_path)

    warnings, suggestions = _build_warnings_and_suggestions(permission_mode, log_path, log_context, worktree_path)

    # Load review feedback from per-cycle files
    review_feedback = _load_review_feedback(Path(worktree_path) if worktree_path else None)

    # Augment with session analysis from run manifest
    analysis_headline, analysis_detail, analysis_suggestions = _load_analysis_from_worktree(
        Path(worktree_path) if worktree_path else None,
        session_output=session_output,
    )

    return SessionFailureDiagnosis(
        issue_number=issue_number,
        ai_system=ai_system,
        permission_mode=permission_mode,
        worktree_path=str(worktree_path) if worktree_path else None,
        log_path=str(log_path) if log_path else None,
        log_exists=log_path.exists() if log_path else False,
        log_context=log_context,
        history_status=history_entry.status if history_entry else None,
        history_reason=history_entry.status_reason if history_entry else None,
        warnings=warnings,
        suggestions=suggestions,
        review_feedback=review_feedback,
        analysis_headline=analysis_headline,
        analysis_detail=analysis_detail,
        analysis_suggestions=analysis_suggestions,
    )
