"""Registry for AI system log providers.

Uses data-driven configuration from ai_systems.yaml to support multiple AI systems.
New AI systems can be added by updating the config file, no code changes required.
"""

import glob
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...infra.ai_systems_config import AISystemConfig, AISystemsConfig

logger = logging.getLogger(__name__)


class DataDrivenLogProvider:
    """Generic log provider that uses AI system config for log discovery and parsing."""

    def __init__(self, config: "AISystemConfig", systems_config: "AISystemsConfig"):
        self._config = config
        self._systems_config = systems_config

    @property
    def ai_system(self) -> str:
        return self._config.name

    def get_log_path(self, worktree_path: Path, session_name: str) -> Path | None:
        """Find the log file for this session using the configured pattern."""
        if not self._config.log_pattern:
            return None

        # Resolve the pattern with variables
        pattern = self._systems_config.resolve_log_pattern(
            self._config.log_pattern,
            worktree_path,
        )

        # Handle glob patterns
        if "*" in pattern:
            matches = glob.glob(pattern)
            if not matches:
                logger.debug("[LOG] No files matching pattern: %s", pattern)
                return None
            # Return most recently modified file
            matches.sort(key=lambda f: Path(f).stat().st_mtime, reverse=True)
            return Path(matches[0])
        else:
            path = Path(pattern)
            return path if path.exists() else None

    def get_failure_context(self, log_path: Path, lines: int = 100) -> str | None:
        """Extract failure context from the log file."""
        if not log_path.exists():
            return None

        log_format = self._config.log_format
        try:
            if log_format == "jsonl":
                return self._parse_jsonl_log(log_path, lines)
            elif log_format == "json":
                return self._parse_json_log(log_path, lines)
            elif log_format == "markdown":
                return self._parse_markdown_log(log_path, lines)
            else:
                return self._parse_text_log(log_path, lines)
        except Exception as e:
            logger.warning("[LOG] Failed to parse log %s: %s", log_path, e)
            return f"Failed to parse log: {e}"

    def _read_jsonl_entries(self, log_path: Path) -> list[dict] | str:
        """Read and parse JSONL entries from a log file.

        Returns:
            List of parsed entries, or error string on failure.
        """
        entries = []
        try:
            with open(log_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            return f"Failed to read log: {e}"
        return entries

    def _build_jsonl_context(self, entries: list[dict]) -> list[str]:
        """Build context parts from JSONL entries."""
        context_parts = []

        # Look for errors
        errors = self._extract_errors_from_entries(entries)
        if errors:
            context_parts.append("## Errors Found")
            context_parts.extend(errors[:10])

        # Look for permission issues (common failure mode)
        permission_issues = self._extract_permission_issues(entries)
        if permission_issues:
            context_parts.append("\n## Permission Issues")
            context_parts.extend(permission_issues[:5])
            context_parts.append("\nTIP: Consider setting provider_args.permission_mode: bypassPermissions in your agent config")

        # Get recent activity
        recent = self._extract_recent_activity(entries, max_items=5)
        if recent:
            context_parts.append("\n## Recent Activity")
            context_parts.extend(recent)

        # Check for completion marker
        if self._config.completion_marker:
            found = self._check_completion_marker(entries)
            if found:
                context_parts.append(f"\n## Completion: {found}")
            else:
                context_parts.append(f"\n## Completion: {self._config.completion_marker} NOT CALLED")

        return context_parts

    def _parse_jsonl_log(self, log_path: Path, max_lines: int) -> str:
        """Parse JSONL log file (Claude, Codex)."""
        result = self._read_jsonl_entries(log_path)
        if isinstance(result, str):
            return result  # Error message
        entries = result

        if not entries:
            return "Log file is empty"

        context_parts = self._build_jsonl_context(entries)
        return "\n".join(context_parts) if context_parts else "No specific failure context found"

    def _parse_json_log(self, log_path: Path, max_lines: int) -> str:
        """Parse JSON log file (Gemini)."""
        try:
            with open(log_path, "r") as f:
                data = json.load(f)
        except Exception as e:
            return f"Failed to read JSON log: {e}"

        # Extract relevant info from Gemini format
        context_parts = []
        if isinstance(data, dict):
            if "messages" in data:
                messages = data["messages"][-5:]  # Last 5 messages
                context_parts.append("## Recent Messages")
                for msg in messages:
                    role = msg.get("role", "unknown")
                    content = str(msg.get("content", ""))[:100]
                    context_parts.append(f"- {role}: {content}...")
            if "error" in data:
                context_parts.append(f"\n## Error: {data['error']}")

        return "\n".join(context_parts) if context_parts else "No specific failure context found"

    def _parse_markdown_log(self, log_path: Path, max_lines: int) -> str:
        """Parse markdown log file (Aider)."""
        try:
            with open(log_path, "r") as f:
                content = f.read()
        except Exception as e:
            return f"Failed to read markdown log: {e}"

        # Get last N lines
        lines = content.strip().split("\n")
        recent_lines = lines[-max_lines:] if len(lines) > max_lines else lines

        # Look for errors in the markdown
        error_lines = [l for l in recent_lines if any(p in l.lower() for p in self._config.error_patterns)]

        context_parts = []
        if error_lines:
            context_parts.append("## Errors Found")
            context_parts.extend([f"- {l}" for l in error_lines[:10]])

        context_parts.append("\n## Recent Activity (last lines)")
        context_parts.extend(recent_lines[-20:])

        return "\n".join(context_parts)

    def _parse_text_log(self, log_path: Path, max_lines: int) -> str:
        """Parse plain text log file."""
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
        except Exception as e:
            return f"Failed to read log: {e}"

        recent_lines = lines[-max_lines:] if len(lines) > max_lines else lines
        return "".join(recent_lines)

    def _extract_errors_from_entries(self, entries: list[dict]) -> list[str]:
        """Extract error messages from log entries."""
        errors = []
        error_patterns = self._config.error_patterns or ["error"]

        for entry in entries:
            entry_str = str(entry).lower()
            if any(p in entry_str for p in error_patterns):
                # Try to extract a meaningful message
                msg = entry.get("message") or entry.get("error") or entry.get("content")
                if msg:
                    errors.append(f"- {str(msg)[:200]}")
                elif entry.get("type") == "error":
                    errors.append(f"- {entry}")

            # Check for tool errors
            if entry.get("type") == "tool_result":
                result = entry.get("result", {})
                if isinstance(result, dict) and result.get("is_error"):
                    errors.append(f"- Tool error: {result.get('content', 'unknown')[:100]}")

        return errors

    def _extract_permission_issues(self, entries: list[dict]) -> list[str]:
        """Extract permission-related issues."""
        issues = []
        permission_keywords = ["permission", "denied", "not allowed", "haven't granted", "approval"]

        for entry in entries:
            content = str(entry)
            if any(kw in content.lower() for kw in permission_keywords):
                msg = entry.get("message") or entry.get("content") or str(entry)[:200]
                if msg and msg not in issues:
                    issues.append(f"- {msg}")
        return issues

    def _extract_recent_activity(self, entries: list[dict], max_items: int = 5) -> list[str]:
        """Extract recent activity summary."""
        recent = []
        for entry in entries[-max_items * 2:]:
            entry_type = entry.get("type", "unknown")
            if entry_type in ("tool_use", "tool_result", "assistant", "user"):
                summary = self._summarize_entry(entry)
                if summary:
                    recent.append(f"- {summary}")
        return recent[-max_items:]

    def _summarize_entry(self, entry: dict) -> str | None:
        """Create a one-line summary of an entry."""
        entry_type = entry.get("type", "unknown")
        if entry_type == "tool_use":
            return f"Tool: {entry.get('name', 'unknown')}"
        if entry_type == "tool_result":
            result = entry.get("result", {})
            is_error = result.get("is_error", False) if isinstance(result, dict) else False
            return f"Tool result: {'ERROR' if is_error else 'OK'}"
        if entry_type == "assistant":
            content = entry.get("content", "")
            if isinstance(content, str):
                return f"Assistant: {content[:50]}..." if len(content) > 50 else f"Assistant: {content}"
        if entry_type == "user":
            return "User input"
        return None

    def _check_completion_marker(self, entries: list[dict]) -> str | None:
        """Check if completion marker was called."""
        marker = self._config.completion_marker
        if not marker:
            return None

        for entry in reversed(entries):
            content = str(entry).lower()
            if marker.lower() in content:
                # Try to extract outcome
                if "completed" in content:
                    return "completed"
                if "blocked" in content:
                    return "blocked"
                if "needs_human" in content:
                    return "needs_human"
                return "called"
        return None


def get_log_provider(
    ai_system: str | None,
    project_root: Path | None = None,
) -> DataDrivenLogProvider | None:
    """Get a log provider for an AI system.

    Args:
        ai_system: AI system identifier (e.g., 'claude-code', 'codex')
        project_root: Project root for loading config

    Returns:
        Log provider instance, or None if unknown system.
    """
    from ...infra.ai_systems_config import get_ai_systems_config

    config = get_ai_systems_config(project_root)

    # Use default if not specified
    system_name = ai_system or config.default_ai_system
    system_config = config.get_system(system_name)

    if not system_config:
        logger.debug("[LOG] Unknown AI system: %s", system_name)
        return None

    return DataDrivenLogProvider(system_config, config)


def get_failure_context_for_session(
    worktree_path: Path,
    session_name: str,
    ai_system: str | None = None,
    terminal_output: str | None = None,
    command: str | None = None,
    project_root: Path | None = None,
) -> str | None:
    """Convenience function to get failure context for a session.

    Tries multiple detection methods to find the AI system if not specified.

    Args:
        worktree_path: Path to the worktree
        session_name: Session identifier
        ai_system: Optional explicit AI system
        terminal_output: Optional terminal output for detection
        command: Optional command for detection
        project_root: Project root for loading config

    Returns:
        Failure context string, or None if unavailable.
    """
    from ...infra.ai_systems_config import get_ai_systems_config

    config = get_ai_systems_config(project_root)

    # Try explicit ai_system first
    detected = ai_system

    # Try detection from terminal output
    if not detected and terminal_output:
        detected = config.detect_from_tags(terminal_output)

    # Try detection from command
    if not detected and command:
        detected = config.detect_from_command(command)

    # Use default
    if not detected:
        detected = config.default_ai_system

    provider = get_log_provider(detected, project_root)
    if not provider:
        return None

    log_path = provider.get_log_path(worktree_path, session_name)
    if not log_path:
        return f"No {detected} log found for worktree: {worktree_path}"

    return provider.get_failure_context(log_path)
