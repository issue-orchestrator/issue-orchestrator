"""Utility functions for issue orchestrator."""


def format_issue_reference(issue_number: int) -> str:
    """Format an issue number as a reference string.

    Args:
        issue_number: The GitHub issue number

    Returns:
        A formatted issue reference string (e.g., "#156")
    """
    return f"#{issue_number}"


def parse_issue_reference(reference: str) -> int | None:
    """Parse an issue reference string to extract the issue number.

    Args:
        reference: An issue reference string (e.g., "#156")

    Returns:
        The issue number, or None if the reference is invalid
    """
    if not reference or not reference.startswith("#"):
        return None

    try:
        return int(reference[1:])
    except ValueError:
        return None
