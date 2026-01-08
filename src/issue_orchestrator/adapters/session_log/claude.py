"""Claude Code log provider.

Claude Code stores session logs in:
  ~/.claude/projects/{escaped-worktree-path}/*.jsonl

The escaped path converts /foo/bar to -foo-bar.
"""

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ClaudeLogProvider:
    """Log provider for Claude Code sessions."""

    @property
    def ai_system(self) -> str:
        return "claude-code"

    def _escape_path(self, worktree_path: Path) -> str:
        """Convert worktree path to Claude's escaped format.

        Example: /Users/bruce/project -> -Users-bruce-project
        """
        path_str = str(worktree_path.resolve())
        return "-" + path_str.lstrip("/").replace("/", "-")

    def get_log_path(self, worktree_path: Path, session_name: str) -> Path | None:
        """Find the most recent Claude log file for this worktree.

        Args:
            worktree_path: Path to the worktree
            session_name: Session identifier (not used for Claude - logs keyed by worktree)

        Returns:
            Path to the most recent .jsonl log file, or None if not found.
        """
        escaped = self._escape_path(worktree_path)
        projects_dir = Path.home() / ".claude" / "projects" / escaped

        if not projects_dir.exists():
            logger.debug("[CLAUDE_LOG] Projects dir not found: %s", projects_dir)
            return None

        # Find the most recent .jsonl file
        jsonl_files = list(projects_dir.glob("*.jsonl"))
        if not jsonl_files:
            logger.debug("[CLAUDE_LOG] No .jsonl files in: %s", projects_dir)
            return None

        # Sort by modification time, most recent first
        jsonl_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        log_path = jsonl_files[0]
        logger.debug("[CLAUDE_LOG] Found log: %s", log_path)
        return log_path

    def get_failure_context(self, log_path: Path, lines: int = 100) -> str | None:
        """Extract failure context from a Claude log file.

        Parses the JSONL file looking for errors, permission issues, and
        the final messages before session end.

        Args:
            log_path: Path to the .jsonl log file
            lines: Maximum lines to include in context

        Returns:
            Human-readable failure context string.
        """
        if not log_path.exists():
            return None

        try:
            entries = self._parse_log_file(log_path)
            if not entries:
                return None

            context_parts: list[str] = []

            # Look for specific failure patterns
            errors = self._extract_errors(entries)
            if errors:
                context_parts.append("## Errors Found")
                context_parts.extend(errors[:10])  # Limit errors

            # Check for permission issues
            permission_issues = self._extract_permission_issues(entries)
            if permission_issues:
                context_parts.append("\n## Permission Issues")
                context_parts.extend(permission_issues[:5])
                context_parts.append("\nTIP: Consider using --permission-mode bypassPermissions or acceptEdits")

            # Get last few messages for context
            recent = self._extract_recent_activity(entries, max_items=5)
            if recent:
                context_parts.append("\n## Recent Activity")
                context_parts.extend(recent)

            # Check if agent-done was called
            agent_done = self._check_agent_done(entries)
            if agent_done:
                context_parts.append(f"\n## Agent Done: {agent_done}")
            else:
                context_parts.append("\n## Agent Done: NOT CALLED (session may have crashed)")

            if context_parts:
                return "\n".join(context_parts)

            return "No specific failure context found in log"

        except Exception as e:
            logger.warning("[CLAUDE_LOG] Failed to parse log: %s", e)
            return f"Failed to parse log: {e}"

    def _parse_log_file(self, log_path: Path) -> list[dict[str, Any]]:
        """Parse JSONL log file into list of entries."""
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
            logger.warning("[CLAUDE_LOG] Error reading log: %s", e)
        return entries

    def _extract_errors(self, entries: list[dict[str, Any]]) -> list[str]:
        """Extract error messages from log entries."""
        errors = []
        for entry in entries:
            # Check for error type entries
            if entry.get("type") == "error":
                msg = entry.get("message") or entry.get("error") or str(entry)
                errors.append(f"- {msg}")

            # Check for tool errors
            if entry.get("type") == "tool_result":
                result = entry.get("result", {})
                if isinstance(result, dict) and result.get("is_error"):
                    errors.append(f"- Tool error: {result.get('content', 'unknown')}")

        return errors

    def _extract_permission_issues(self, entries: list[dict[str, Any]]) -> list[str]:
        """Extract permission-related issues from log entries."""
        issues = []
        for entry in entries:
            content = str(entry)
            if any(pattern in content.lower() for pattern in [
                "permission",
                "denied",
                "not allowed",
                "haven't granted",
                "approval",
            ]):
                # Try to extract meaningful message
                msg = entry.get("message") or entry.get("content") or str(entry)[:200]
                if msg and msg not in issues:
                    issues.append(f"- {msg}")
        return issues

    def _extract_recent_activity(self, entries: list[dict[str, Any]], max_items: int = 5) -> list[str]:
        """Extract recent activity summary from log entries."""
        recent = []
        # Look at last N entries
        for entry in entries[-max_items * 2:]:
            entry_type = entry.get("type", "unknown")
            if entry_type in ("tool_use", "tool_result", "assistant", "user"):
                summary = self._summarize_entry(entry)
                if summary:
                    recent.append(f"- {summary}")
        return recent[-max_items:]

    def _summarize_entry(self, entry: dict[str, Any]) -> str | None:
        """Create a one-line summary of a log entry."""
        entry_type = entry.get("type", "unknown")

        if entry_type == "tool_use":
            tool_name = entry.get("name", "unknown")
            return f"Tool: {tool_name}"

        if entry_type == "tool_result":
            result = entry.get("result", {})
            if isinstance(result, dict):
                is_error = result.get("is_error", False)
                return f"Tool result: {'ERROR' if is_error else 'OK'}"
            return "Tool result"

        if entry_type == "assistant":
            content = entry.get("content", "")
            if isinstance(content, str):
                return f"Assistant: {content[:50]}..." if len(content) > 50 else f"Assistant: {content}"
            return "Assistant message"

        if entry_type == "user":
            return "User input"

        return None

    def _check_agent_done(self, entries: list[dict[str, Any]]) -> str | None:
        """Check if agent-done was called and extract outcome."""
        for entry in reversed(entries):
            content = str(entry)
            if "agent-done" in content.lower() or "agent_done" in content.lower():
                # Try to extract outcome
                if "completed" in content.lower():
                    return "completed"
                if "blocked" in content.lower():
                    return "blocked"
                if "needs_human" in content.lower():
                    return "needs_human"
                return "called (outcome unclear)"
        return None
