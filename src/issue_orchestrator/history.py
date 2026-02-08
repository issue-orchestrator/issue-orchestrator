"""Helpers for session history views."""

from collections.abc import Sequence
from typing import Protocol, TypeVar


class _HasIssueNumber(Protocol):
    issue_number: int


_HistoryEntry = TypeVar("_HistoryEntry", bound=_HasIssueNumber)


def latest_history_entries_by_issue(
    session_history: Sequence[_HistoryEntry],
    limit: int = 50,
) -> list[_HistoryEntry]:
    """Return most recent history entries, deduplicated by issue number."""
    latest: list[_HistoryEntry] = []
    seen_issue_numbers: set[int] = set()
    for entry in reversed(session_history):
        issue_number = int(entry.issue_number)
        if issue_number in seen_issue_numbers:
            continue
        seen_issue_numbers.add(issue_number)
        latest.append(entry)
        if len(latest) >= limit:
            break
    return latest
