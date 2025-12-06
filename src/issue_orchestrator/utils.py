"""Utility functions for issue-orchestrator."""

import re
from pathlib import Path


def slugify(text: str, max_length: int = 50) -> str:
    """Convert text to a URL-safe slug.

    Args:
        text: The text to slugify
        max_length: Maximum length of the slug (default 50)

    Returns:
        A URL-safe slug suitable for git branch names and filenames

    Examples:
        >>> slugify("[TEST] Simple backend task")
        'test-simple-backend-task'
        >>> slugify("Fix: User authentication bug #123")
        'fix-user-authentication-bug-123'
    """
    # Convert to lowercase
    slug = text.lower()

    # Strip leading/trailing whitespace first
    slug = slug.strip()

    # Replace spaces with hyphens
    slug = re.sub(r'\s+', '-', slug)

    # Replace any remaining non-word characters (except hyphens) with hyphens
    slug = re.sub(r'[^\w-]+', '-', slug)

    # Collapse consecutive hyphens
    slug = re.sub(r'-+', '-', slug)

    # Remove leading and trailing hyphens
    slug = slug.strip('-')

    # Truncate to max length
    slug = slug[:max_length]

    # Remove trailing hyphens again in case truncation left some
    slug = slug.rstrip('-')

    return slug


def ensure_directory_exists(path: Path) -> None:
    """Ensure a directory exists, creating it if necessary.

    Args:
        path: The directory path to ensure exists
    """
    path.mkdir(parents=True, exist_ok=True)


def format_list_for_display(items: list[str], separator: str = ", ") -> str:
    """Format a list of items for display output.

    Args:
        items: The list of items to format
        separator: The separator between items

    Returns:
        A formatted string representation of the list

    Examples:
        >>> format_list_for_display(['a', 'b', 'c'])
        'a, b, c'
        >>> format_list_for_display(['x', 'y'])
        'x, y'
    """
    return separator.join(items)
