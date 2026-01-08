"""Centralized terminal session naming conventions.

This module defines the mapping between:
- terminal_id: Internal identifier like "issue-123", "review-456"
- tab_name: Display name in terminal like "#123 Title"

IMPORTANT: All terminal adapters must use these functions to ensure
consistent naming across the codebase. Do not hardcode naming patterns.
"""

from dataclasses import dataclass
from enum import Enum


class SessionType(Enum):
    """Types of terminal sessions."""
    ISSUE = "issue"
    REVIEW = "review"
    REWORK = "rework"
    TRIAGE = "triage"


@dataclass(frozen=True)
class ParsedSessionName:
    """Result of parsing a session name/terminal_id."""
    session_type: SessionType
    number: int

    @property
    def terminal_id(self) -> str:
        """Get the terminal_id (e.g., 'issue-123')."""
        return f"{self.session_type.value}-{self.number}"

    @property
    def iterm_tab_prefix(self) -> str:
        """Get the iTerm tab name prefix (e.g., '#123')."""
        return f"#{self.number}"


def terminal_id(session_type: str | SessionType, number: int) -> str:
    """Generate terminal_id from session type and number.

    Args:
        session_type: Type of session ("issue", "review", etc.) or SessionType enum
        number: Issue or PR number

    Returns:
        Terminal ID like "issue-123" or "review-456"

    Example:
        >>> terminal_id("issue", 123)
        'issue-123'
        >>> terminal_id(SessionType.REVIEW, 456)
        'review-456'
    """
    if isinstance(session_type, SessionType):
        session_type = session_type.value
    return f"{session_type}-{number}"


def parse_terminal_id(tid: str) -> ParsedSessionName | None:
    """Parse a terminal_id into its components.

    Args:
        tid: Terminal ID like "issue-123", "review-456"

    Returns:
        ParsedSessionName or None if invalid format

    Example:
        >>> result = parse_terminal_id("issue-123")
        >>> result.session_type
        SessionType.ISSUE
        >>> result.number
        123
    """
    if "-" not in tid:
        return None

    try:
        parts = tid.rsplit("-", 1)
        session_type_str = parts[0]
        number = int(parts[1])

        # Map to SessionType enum
        try:
            session_type = SessionType(session_type_str)
        except ValueError:
            return None

        return ParsedSessionName(session_type=session_type, number=number)
    except (ValueError, IndexError):
        return None


def iterm_tab_name(number: int, title: str | None = None, max_title_length: int = 20) -> str:
    """Generate iTerm tab name from issue/PR number and optional title.

    Args:
        number: Issue or PR number
        title: Optional title to include (will be truncated)
        max_title_length: Maximum length for title portion

    Returns:
        Tab name like "#123" or "#123 Fix authentication bug"

    Example:
        >>> iterm_tab_name(123)
        '#123'
        >>> iterm_tab_name(123, "Fix authentication bug")
        '#123 Fix authentication '
    """
    if title:
        short_title = title[:max_title_length]
        return f"#{number} {short_title}"
    return f"#{number}"


def iterm_tab_prefix(number: int) -> str:
    """Get the iTerm tab name prefix for matching.

    This is used for:
    - Finding tabs by issue number
    - Focusing sessions from web UI

    Args:
        number: Issue or PR number

    Returns:
        Tab prefix like "#123"

    Example:
        >>> iterm_tab_prefix(123)
        '#123'
    """
    return f"#{number}"


def number_from_terminal_id(tid: str) -> int | None:
    """Extract the number from a terminal_id.

    Args:
        tid: Terminal ID like "issue-123"

    Returns:
        The number (123) or None if invalid

    Example:
        >>> number_from_terminal_id("issue-123")
        123
        >>> number_from_terminal_id("review-456")
        456
    """
    parsed = parse_terminal_id(tid)
    return parsed.number if parsed else None
