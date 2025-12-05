"""Formatting utilities for issue orchestrator."""


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human readable format.

    Args:
        seconds: Duration in seconds

    Returns:
        Human-readable duration string

    Examples:
        >>> format_duration(30)
        '30s'
        >>> format_duration(120)
        '2m'
        >>> format_duration(3600)
        '1h'
        >>> format_duration(3661)
        '1h 1m'
    """
    if seconds < 60:
        return f"{seconds}s"

    minutes = seconds // 60
    remaining_seconds = seconds % 60

    if minutes < 60:
        if remaining_seconds:
            return f"{minutes}m {remaining_seconds}s"
        return f"{minutes}m"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if remaining_minutes:
        return f"{hours}h {remaining_minutes}m"
    return f"{hours}h"


def format_issue_number(number: int) -> str:
    """Format an issue number with # prefix.

    Args:
        number: Issue number

    Returns:
        Formatted issue number string

    Examples:
        >>> format_issue_number(232)
        '#232'
    """
    return f"#{number}"


def truncate_string(text: str, max_length: int = 80, suffix: str = "...") -> str:
    """Truncate a string to a maximum length.

    Args:
        text: Text to truncate
        max_length: Maximum length
        suffix: Suffix to add if truncated

    Returns:
        Truncated string

    Examples:
        >>> truncate_string("hello world", 5)
        'he...'
        >>> truncate_string("short", 10)
        'short'
    """
    if len(text) <= max_length:
        return text

    truncate_at = max_length - len(suffix)
    return text[:truncate_at] + suffix
