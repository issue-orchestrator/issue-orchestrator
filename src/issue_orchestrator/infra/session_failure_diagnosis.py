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

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
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
        }


def create_session_failure_diagnosis(
    issue_number: int,
    session_history: list,
    active_sessions: list,
    config: "Config",
    agents: dict,
) -> SessionFailureDiagnosis:
    """Create a failure diagnosis for a session.

    This extracts the diagnosis logic that was in web.py's get_failure_diagnosis
    endpoint, allowing it to be called from the orchestrator.

    Args:
        issue_number: The issue number to diagnose
        session_history: List of SessionHistoryEntry from orchestrator state
        active_sessions: List of active Sessions from orchestrator state
        config: Orchestrator config
        agents: Dict of agent configs

    Returns:
        SessionFailureDiagnosis with all diagnostic info
    """
    from ..adapters.session_log.registry import get_log_provider
    from ..ports.session_log import detect_ai_system_from_command

    # Find the session history entry for this issue
    history_entry = None
    for entry in reversed(session_history):
        if entry.issue_number == issue_number:
            history_entry = entry
            break

    # Find worktree path - check active sessions, history, or construct it
    worktree_path: Path | None = None
    agent_config = None

    # Check active sessions first
    for session in active_sessions:
        if session.issue.number == issue_number:
            worktree_path = session.worktree_path
            agent_config = session.agent_config
            break

    # If not found, try to construct from config
    if not worktree_path:
        worktree_base = config.worktree_base or Path.cwd().parent
        repo_name = config.repo.split("/")[-1] if config.repo else "unknown"
        worktree_path = Path(worktree_base) / f"{repo_name}-{issue_number}"

    # Detect AI system and get provider
    ai_system = "claude-code"  # default
    permission_mode = "unknown"
    if agent_config:
        ai_system = detect_ai_system_from_command(agent_config.command) or "claude-code"
        permission_mode = agent_config.permission_mode
    elif history_entry:
        # Try to get agent config from history
        agent_label = history_entry.agent_type
        if agent_label in agents:
            cfg = agents[agent_label]
            ai_system = detect_ai_system_from_command(cfg.command) or "claude-code"
            permission_mode = cfg.permission_mode

    provider = get_log_provider(ai_system)

    # Get log path and context
    log_path: Path | None = None
    log_context: str | None = None
    if provider and worktree_path:
        log_path = provider.get_log_path(Path(worktree_path), f"issue-{issue_number}")
        if log_path:
            log_context = provider.get_failure_context(log_path)

    # Build warnings and suggestions
    warnings: list[str] = []
    suggestions: list[str] = []

    if permission_mode == "default":
        warnings.append(
            "permission_mode is 'default' - Claude prompts for permissions in non-interactive mode"
        )
        suggestions.append(
            "Add 'permission_mode: bypassPermissions' to your agent config in YAML"
        )

    if not log_path or not (log_path and log_path.exists()):
        warnings.append("No AI session log found for this issue")
        suggestions.append(
            f"Check ~/.claude/projects/ for logs related to {worktree_path}"
        )

    if log_context and "permission" in log_context.lower():
        warnings.append("Permission-related errors detected in log")
        suggestions.append(
            "Consider using 'permission_mode: bypassPermissions' for non-interactive sessions"
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
    )
