"""Role prompt-readiness and artifact freshness helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..domain.models import AgentConfig
from .agent_runner_providers import get_provider, is_valid_provider

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
    provider_name = _agent_provider_name(agent)
    if provider_name is None or not is_valid_provider(provider_name):
        return False
    return get_provider(provider_name).needs_fresh_prompt_process(**agent.provider_args)


def _agent_provider_name(agent: AgentConfig) -> str | None:
    """Provider identity for exchange-session classification."""
    provider = agent.provider if agent.provider else agent.ai_system
    if not provider:
        return None
    return provider.strip().lower()


@dataclass(frozen=True)
class RoleAttemptWorkspace:
    """Owns the on-disk artifact freshness for one role's turn.

    A dead process can leave side artifacts such as ``review-report.md`` or
    ``completion-coder.json`` before writing a valid response. Clearing both
    the response and role-owned side artifacts before each attempt keeps stale
    output from being paired with a later respawned process.
    """

    response_file: Path
    side_artifact_paths: tuple[Path, ...]

    def prepare_for_attempt(self) -> None:
        """Clear the response, prompt inbox, and role side artifacts."""
        self.response_file.unlink(missing_ok=True)
        role_prompt_inbox_path(self.response_file).unlink(missing_ok=True)
        for path in self.side_artifact_paths:
            path.unlink(missing_ok=True)
