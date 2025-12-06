"""Frontend utility functions for the dashboard."""

from datetime import datetime
from typing import Any, Dict


def format_date(date: datetime | str) -> str:
    """Format a date in a human-readable way.

    Args:
        date: A datetime object or ISO format string.

    Returns:
        Formatted date string like "Dec 5, 2025 11:30 PM".
    """
    if isinstance(date, str):
        if not date:
            return ""
        date = datetime.fromisoformat(date.replace('Z', '+00:00'))
    return date.strftime('%b %d, %Y %I:%M %p')


def truncate_text(text: str, max_length: int = 50) -> str:
    """Truncate text to a maximum length.

    Args:
        text: The text to truncate.
        max_length: Maximum length before truncation.

    Returns:
        Truncated text with ellipsis if needed.
    """
    if len(text) <= max_length:
        return text
    return text[:max_length] + '...'


def parse_status_badge_class(status: str) -> str:
    """Get CSS class for a status badge.

    Args:
        status: The status value.

    Returns:
        CSS class name for the badge.
    """
    status_map = {
        'running': 'status-running',
        'completed': 'status-completed',
        'failed': 'status-failed',
        'paused': 'status-paused',
        'pending': 'status-pending',
    }
    return status_map.get(status.lower(), 'status-default')


def format_issue_for_display(issue_data: Dict[str, Any]) -> Dict[str, Any]:
    """Format issue data for frontend display.

    Args:
        issue_data: Raw issue data from GitHub API.

    Returns:
        Formatted issue data for template rendering.
    """
    return {
        'number': issue_data.get('number'),
        'title': truncate_text(issue_data.get('title', ''), max_length=60),
        'full_title': issue_data.get('title', ''),
        'state': issue_data.get('state', 'unknown'),
        'created_at': format_date(issue_data.get('created_at', '')),
        'updated_at': format_date(issue_data.get('updated_at', '')),
        'labels': issue_data.get('labels', []),
        'status_class': parse_status_badge_class(issue_data.get('state', '')),
    }
