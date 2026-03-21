"""Queue cache mutations and eligibility policy.

This module centralizes queue eligibility and mutations so call sites
cannot bypass scope policy when updating ``state.cached_queue_issues``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..domain.models import OrchestratorState
    from ..ports.issue import Issue


class QueueMutationStatus(str, Enum):
    """Result status for queue cache mutation operations."""

    ACCEPTED = "accepted"
    REJECTED_OUT_OF_SCOPE = "rejected_out_of_scope"
    REJECTED_EXCLUDED = "rejected_excluded"


@dataclass(frozen=True)
class QueueMutationOutcome:
    """Outcome details from queue cache mutations."""

    status: QueueMutationStatus
    in_queue: bool
    updated: bool


_UI_VISIBILITY_STALENESS_SECONDS = 120


class QueueCache:
    """Only writer for queue cache state."""

    def __init__(self, config: "Config", state: "OrchestratorState"):
        self._config = config
        self._state = state

    def replace_from_refresh(self, issues: list["Issue"]) -> list["Issue"]:
        """Replace queue from fetched issues using canonical eligibility policy."""
        queue = [issue for issue in issues if self.evaluate_issue(issue) == QueueMutationStatus.ACCEPTED]
        self._state.cached_queue_issues = queue
        self.prune_refresh_timestamps()
        return queue

    def upsert_refreshed_issue(self, issue: "Issue") -> QueueMutationOutcome:
        """Upsert a refreshed issue while enforcing queue eligibility policy."""
        was_present = any(cached.number == issue.number for cached in self._state.cached_queue_issues)
        self._state.cached_queue_issues = [
            cached for cached in self._state.cached_queue_issues if cached.number != issue.number
        ]
        status = self.evaluate_issue(issue)
        if status == QueueMutationStatus.ACCEPTED:
            self._state.cached_queue_issues.append(issue)
            return QueueMutationOutcome(status=status, in_queue=True, updated=was_present)
        return QueueMutationOutcome(status=status, in_queue=False, updated=False)

    def remove_issue(self, issue_number: int) -> None:
        """Remove issue from cached queue and refresh metadata."""
        self._state.cached_queue_issues = [
            issue for issue in self._state.cached_queue_issues if issue.number != issue_number
        ]
        clear_issue_refresh(self._state, issue_number)
        self.prune_refresh_timestamps()

    def evaluate_issue(self, issue: "Issue") -> QueueMutationStatus:
        """Evaluate whether issue can be in queue cache."""
        if not _matches_scope(self._config, issue):
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        excluded_numbers = {entry.issue_number for entry in self._state.session_history}
        excluded_numbers.update(session.issue.number for session in self._state.active_sessions)
        if issue.number in excluded_numbers:
            return QueueMutationStatus.REJECTED_EXCLUDED

        if self._config.filtering.issue and issue.number != self._config.filtering.issue:
            return QueueMutationStatus.REJECTED_OUT_OF_SCOPE

        return QueueMutationStatus.ACCEPTED

    def prune_refresh_timestamps(self) -> None:
        """Prune refresh timestamp map to currently tracked issue IDs."""
        if not self._state.issue_refresh_timestamps and not self._state.issue_last_refreshed_at:
            return
        keep_numbers = {issue.number for issue in self._state.cached_queue_issues}
        keep_numbers.update(session.issue.number for session in self._state.active_sessions)
        keep_numbers.update(entry.issue_number for entry in self._state.session_history)
        keep_numbers.update(self._visible_issue_numbers())
        self._state.issue_refresh_timestamps = {
            issue_number: refreshed_at
            for issue_number, refreshed_at in self._state.issue_refresh_timestamps.items()
            if issue_number in keep_numbers
        }
        self._state.issue_last_refreshed_at = {
            issue_number: refreshed_at
            for issue_number, refreshed_at in self._state.issue_last_refreshed_at.items()
            if issue_number in keep_numbers
        }

    def _visible_issue_numbers(self) -> set[int]:
        """Return issues that the UI is actively displaying and should keep fresh."""
        if self._state.ui_visible_updated_at <= 0:
            return set()
        if (time.time() - self._state.ui_visible_updated_at) > _UI_VISIBILITY_STALENESS_SECONDS:
            return set()
        return set(self._state.ui_visible_issue_numbers)


def record_issue_refreshes(
    state: "OrchestratorState",
    refreshed_numbers: set[int],
    refreshed_at: float,
) -> None:
    """Record freshness for tracked issues in both dashboard freshness maps."""
    if not refreshed_numbers:
        return
    for issue_number in refreshed_numbers:
        state.issue_refresh_timestamps[issue_number] = refreshed_at
        state.issue_last_refreshed_at[issue_number] = refreshed_at


def clear_issue_refresh(state: "OrchestratorState", issue_number: int) -> None:
    """Clear freshness metadata for an issue from both dashboard freshness maps."""
    state.issue_refresh_timestamps.pop(issue_number, None)
    state.issue_last_refreshed_at.pop(issue_number, None)


def _matches_scope(config: "Config", issue: "Issue") -> bool:
    """Apply label/milestone/exclude-label scope checks for an issue."""
    if issue.state.lower() == "closed":
        return False
    if config.filtering.label and config.filtering.label not in issue.labels:
        return False
    milestones = config.get_filter_milestones()
    if milestones and issue.milestone not in milestones:
        return False
    issue_filter = config.get_issue_filter()
    if not issue_filter.apply([issue]):
        return False
    return True
