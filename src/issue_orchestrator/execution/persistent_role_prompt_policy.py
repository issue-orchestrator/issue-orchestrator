"""Role prompt-readiness and artifact freshness helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.models import AgentConfig

FRESH_CODEX_PROMPT_PROCESS_TRIGGER = (
    "interactive codex process completed a prior turn; "
    "respawning before next prompt to avoid stale TUI input state"
)


def role_prompt_inbox_path(response_file: Path) -> Path:
    """Stable role-local prompt inbox next to the response file.

    The role process is launched with its worktree as cwd, while its response
    file lives under ``<worktree>/.issue-orchestrator``. Writing each turn's
    full prompt beside that response file keeps the prompt readable inside the
    role's sandbox and lets the PTY carry only a short wake-up message.

    This inbox is intentionally transient and overwritten per turn; the durable
    per-turn prompt remains under ``<exchange_dir>/turns/``.
    """
    return response_file.with_name("review-exchange-turn-prompt.md")


def role_session_needs_fresh_prompt_process(agent: AgentConfig) -> bool:
    """Whether a completed role turn should force a fresh process next prompt."""
    provider = agent.resolve_launch_provider() or agent.ai_system
    if (provider or "").strip().lower() != "codex":
        return False
    execution_mode = (
        str(agent.provider_args.get("execution_mode", "interactive")).strip().lower()
    )
    return execution_mode in {"interactive", "tui"}


@dataclass(frozen=True)
class RoleAttemptWorkspace:
    """Owns the on-disk artifact freshness for one role's turn."""

    response_file: Path
    side_artifact_paths: tuple[Path, ...]

    def prepare_for_attempt(self) -> None:
        """Clear the response, prompt inbox, and role side artifacts."""
        self.response_file.unlink(missing_ok=True)
        role_prompt_inbox_path(self.response_file).unlink(missing_ok=True)
        for path in self.side_artifact_paths:
            path.unlink(missing_ok=True)
