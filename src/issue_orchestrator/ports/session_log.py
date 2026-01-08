"""Session log provider port for retrieving AI agent logs.

This port defines the interface for finding and extracting logs from different
AI systems (Claude, Codex, Gemini, etc.). The orchestrator calls these methods
to surface failure context to users.

Each AI system stores logs differently:
- Claude: ~/.claude/projects/{escaped-worktree}/*.jsonl
- Codex: TBD
- Gemini: TBD

This abstraction keeps AI system details out of the core.
"""

from pathlib import Path
from typing import Protocol


class SessionLogProvider(Protocol):
    """Port for retrieving AI session logs.

    Implementations know where each AI system stores its logs and how to
    parse them for relevant failure context.
    """

    @property
    def ai_system(self) -> str:
        """Return the AI system identifier (e.g., 'claude-code', 'codex')."""
        ...

    def get_log_path(self, worktree_path: Path, session_name: str) -> Path | None:
        """Find the log file for this session.

        Args:
            worktree_path: Path to the worktree where the session ran
            session_name: Session identifier (e.g., 'issue-123')

        Returns:
            Path to the log file, or None if not found.
        """
        ...

    def get_failure_context(self, log_path: Path, lines: int = 100) -> str | None:
        """Extract relevant failure context from the log.

        Args:
            log_path: Path to the log file
            lines: Maximum lines to return

        Returns:
            Human-readable failure context, or None if unavailable.
        """
        ...


def detect_ai_system_from_output(output: str) -> str | None:
    """Detect the AI system from terminal output.

    Looks for signature patterns in the output to identify which AI system
    is running. This allows automatic detection without explicit config.

    Args:
        output: Terminal output text

    Returns:
        AI system identifier (e.g., 'claude-code', 'codex'), or None if unknown.
    """
    if not output:
        return None

    output_lower = output.lower()

    # Claude Code patterns
    if any(pattern in output_lower for pattern in [
        "claude",
        "anthropic",
        "claude-code",
        "claude code",
    ]):
        return "claude-code"

    # Codex/OpenAI patterns
    if any(pattern in output_lower for pattern in [
        "codex",
        "openai",
        "gpt-4",
        "gpt-3",
    ]):
        return "codex"

    # Gemini patterns
    if any(pattern in output_lower for pattern in [
        "gemini",
        "google ai",
        "palm",
    ]):
        return "gemini"

    # Aider patterns
    if "aider" in output_lower:
        return "aider"

    # Cursor patterns
    if "cursor" in output_lower:
        return "cursor"

    return None


def detect_ai_system_from_command(command: str) -> str | None:
    """Detect the AI system from the launch command.

    Parses the command to identify which AI system is being invoked.
    This is a fallback when output detection isn't possible.

    Args:
        command: Shell command used to launch the agent

    Returns:
        AI system identifier, or None if unknown.
    """
    if not command:
        return None

    # Look for known CLI tool names at the start of the command
    # (after any env vars like ORCHESTRATOR_COMPLETION_PATH=...)
    command_stripped = command.strip()

    # Skip env var assignments at the start
    parts = command_stripped.split()
    cmd_start = None
    for part in parts:
        if "=" not in part:
            cmd_start = part.lower()
            break

    if not cmd_start:
        return None

    # Match known CLI tools
    if cmd_start in ("claude", "claude-code"):
        return "claude-code"
    if cmd_start == "codex":
        return "codex"
    if cmd_start == "gemini":
        return "gemini"
    if cmd_start == "aider":
        return "aider"
    if cmd_start == "cursor":
        return "cursor"

    return None
