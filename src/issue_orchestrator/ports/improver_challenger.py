"""Port: what GitHub shows of a challenger's issue, for its approval (#8001)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..domain.tech_lead_approval import LabelEvent
    from .issue import Issue


class ChallengerIssueEvidence(Protocol):
    """Fresh reads about one issue in the improver's outputs repository."""

    def get_issue(self, issue_number: int) -> Issue | None: ...

    def latest_label_event(self, issue_number: int, label: str, *, removed: bool = False) -> LabelEvent | None: ...

    def repository_role(self, login: str) -> str | None: ...

    def issue_closed_on_or_after(self, issue_number: int, timestamp: str) -> bool: ...


__all__ = ["ChallengerIssueEvidence"]
