"""Helpers for session history views."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol, TypeVar

if TYPE_CHECKING:
    from .domain.models import SessionHistoryEntry


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


def issues_held_by_session_history(
    session_history: Sequence["SessionHistoryEntry"],
) -> frozenset[int]:
    """Issues this run must not launch again because a session already ran.

    Every issue with a history entry is held, except one whose latest entry
    is a merged partial PR: that issue has remaining work, and its next slice
    may launch (#7288). The planner and the queue cache both ask this, so the
    two cannot disagree about which issues are schedulable.
    """
    latest: dict[int, "SessionHistoryEntry"] = {}
    for entry in session_history:
        latest[int(entry.issue_number)] = entry
    return frozenset(
        number for number, entry in latest.items() if not entry.partial_pr_merged
    )
