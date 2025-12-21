"""LabelSync - synchronizes labels between desired and actual state.

This module handles the IO of applying label changes to GitHub.
It's idempotent: adding an existing label or removing a missing label is a no-op.

Usage:
    sync = LabelSync(labels=label_set_port, events=event_sink)
    result = sync.sync(issue_number=123, current={"in-progress"}, desired=desired_labels)
"""

import logging
from dataclasses import dataclass
from typing import Set

from ..ports import EventSink, TraceEvent
from ..ports.label_set import LabelSet
from .label_projection import DesiredLabels, compute_label_changes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LabelSyncResult:
    """Result of a label sync operation.

    Attributes:
        issue_number: The issue that was synced
        added: Labels that were added
        removed: Labels that were removed
        errors: Any errors that occurred (label name -> error message)
    """

    issue_number: int
    added: frozenset[str]
    removed: frozenset[str]
    errors: dict[str, str]

    @property
    def success(self) -> bool:
        """Check if sync completed without errors."""
        return len(self.errors) == 0

    @property
    def changed(self) -> bool:
        """Check if any labels were changed."""
        return len(self.added) > 0 or len(self.removed) > 0


class LabelSync:
    """Synchronizes labels between desired and actual state.

    This class handles the IO of applying label changes to GitHub.
    It uses the LabelSet port for actual label operations.
    """

    def __init__(self, labels: LabelSet, events: EventSink):
        """Initialize the sync service.

        Args:
            labels: LabelSet port for label operations
            events: EventSink for trace events
        """
        self.labels = labels
        self.events = events

    def sync(
        self,
        issue_number: int,
        current: Set[str],
        desired: DesiredLabels,
    ) -> LabelSyncResult:
        """Synchronize labels for an issue.

        This is idempotent: adding existing or removing missing labels is safe.

        Args:
            issue_number: The issue number to sync
            current: Set of labels currently on the issue
            desired: DesiredLabels specifying what should change

        Returns:
            LabelSyncResult with details of what changed
        """
        to_add, to_remove = compute_label_changes(current, desired)

        added: set[str] = set()
        removed: set[str] = set()
        errors: dict[str, str] = {}

        # Add labels
        for label in to_add:
            try:
                self.labels.add_label(issue_number, label)
                added.add(label)
                logger.debug(f"[LABEL_SYNC] Added '{label}' to #{issue_number}")
            except Exception as e:
                errors[label] = f"add failed: {e}"
                logger.warning(f"[LABEL_SYNC] Failed to add '{label}' to #{issue_number}: {e}")

        # Remove labels
        for label in to_remove:
            try:
                self.labels.remove_label(issue_number, label)
                removed.add(label)
                logger.debug(f"[LABEL_SYNC] Removed '{label}' from #{issue_number}")
            except Exception as e:
                errors[label] = f"remove failed: {e}"
                logger.warning(f"[LABEL_SYNC] Failed to remove '{label}' from #{issue_number}: {e}")

        result = LabelSyncResult(
            issue_number=issue_number,
            added=frozenset(added),
            removed=frozenset(removed),
            errors=errors,
        )

        # Emit trace event if anything changed
        if result.changed:
            self.events.publish(
                TraceEvent(
                    name="labels.synced",
                    data={
                        "issue_number": issue_number,
                        "added": list(added),
                        "removed": list(removed),
                        "success": result.success,
                    },
                )
            )

        return result

    def sync_add(self, issue_number: int, *labels: str) -> LabelSyncResult:
        """Convenience method to add labels.

        Args:
            issue_number: The issue number
            *labels: Labels to add

        Returns:
            LabelSyncResult
        """
        return self.sync(
            issue_number=issue_number,
            current=set(),  # We don't know current, but add is idempotent
            desired=DesiredLabels.add(*labels),
        )

    def sync_remove(self, issue_number: int, *labels: str) -> LabelSyncResult:
        """Convenience method to remove labels.

        Args:
            issue_number: The issue number
            *labels: Labels to remove

        Returns:
            LabelSyncResult
        """
        return self.sync(
            issue_number=issue_number,
            current=set(labels),  # Assume they exist so they'll be removed
            desired=DesiredLabels.remove(*labels),
        )

    def remove_blocked_labels(self, issue_number: int, current: Set[str]) -> LabelSyncResult:
        """Remove all blocked-* labels from an issue.

        Args:
            issue_number: The issue number
            current: Current labels on the issue

        Returns:
            LabelSyncResult with removed blocked labels
        """
        # Find all blocked-* labels
        blocked_labels = {label for label in current if label.startswith("blocked")}

        if not blocked_labels:
            return LabelSyncResult(
                issue_number=issue_number,
                added=frozenset(),
                removed=frozenset(),
                errors={},
            )

        return self.sync(
            issue_number=issue_number,
            current=current,
            desired=DesiredLabels.remove(*blocked_labels),
        )
