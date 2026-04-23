"""LabelSync - synchronizes labels between desired and actual state.

This module handles the IO of applying label changes to GitHub.
It's idempotent: adding an existing label or removing a missing label is a no-op.

Usage:
    sync = LabelSync(labels=label_set_port, events=event_sink)
    result = sync.sync(issue_number=123, current={"in-progress"}, desired=desired_labels)
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional, Set, FrozenSet

from ..events import EventName
from ..ports import EventSink,  make_trace_event
from ..infra import gh_audit
from ..ports.label_set import LabelSet
from ..ports.pull_request_tracker import PullRequestTracker

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from ..ports.pull_request_tracker import PRInfo


@dataclass(frozen=True)
class DesiredLabels:
    """The set of labels an issue should have."""

    to_add: FrozenSet[str] = field(default_factory=frozenset)
    to_remove: FrozenSet[str] = field(default_factory=frozenset)
    # Labels that must not be on the issue (for explicit cleanup)
    must_not_have: FrozenSet[str] = field(default_factory=frozenset)

    @classmethod
    def add(cls, *labels: str) -> "DesiredLabels":
        """Create a DesiredLabels that adds the given labels."""
        return cls(to_add=frozenset(labels))

    @classmethod
    def remove(cls, *labels: str) -> "DesiredLabels":
        """Create a DesiredLabels that removes the given labels."""
        return cls(to_remove=frozenset(labels))

    @classmethod
    def replace(cls, add: set[str], remove: set[str]) -> "DesiredLabels":
        """Create a DesiredLabels that adds and removes labels."""
        return cls(to_add=frozenset(add), to_remove=frozenset(remove))

    def merge(self, other: "DesiredLabels") -> "DesiredLabels":
        """Merge two DesiredLabels, combining their add/remove sets."""
        return DesiredLabels(
            to_add=self.to_add | other.to_add,
            to_remove=self.to_remove | other.to_remove,
            must_not_have=self.must_not_have | other.must_not_have,
        )


def compute_label_changes(
    current: Set[str],
    desired: DesiredLabels,
) -> tuple[Set[str], Set[str]]:
    """Compute what label changes are needed."""
    to_add: Set[str] = set(desired.to_add - current)
    to_remove: Set[str] = set(desired.to_remove & current)

    # Handle must_not_have patterns (prefix matching)
    for pattern in desired.must_not_have:
        for label in current:
            if label.startswith(pattern):
                to_remove.add(label)

    return to_add, to_remove

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

    def __init__(
        self,
        labels: LabelSet,
        events: EventSink,
        pr_tracker: Optional[PullRequestTracker] = None,
        label_manager: "LabelManager | None" = None,
    ):
        """Initialize the sync service.

        Args:
            labels: LabelSet port for label operations
            events: EventSink for trace events
            pr_tracker: Optional PullRequestTracker for PR operations (used for reconciliation)
            label_manager: Label registry for prefix-aware blocking checks.
        """
        self.labels = labels
        self.events = events
        self.pr_tracker = pr_tracker
        self._label_manager = label_manager

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

        # Emit trace event if anything changed or errors occurred
        if result.changed or result.errors:
            self.events.publish(
                make_trace_event(
                    EventName.LABELS_SYNCED,
                    {
                        "issue_number": issue_number,
                        "added": list(added),
                        "removed": list(removed),
                        "success": result.success,
                        "errors": errors,
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
        # Find all blocking labels (prefix-aware when label_manager is available)
        if self._label_manager:
            blocked_labels = {label for label in current if self._label_manager.is_blocking(label)}
        else:
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

    def reconcile_orphaned_pr_labels(
        self,
        code_review_label: str,
        code_reviewed_label: str | None,
        orchestrator_marker: str,
        is_pr_in_scope: Callable[["PRInfo"], bool] | None = None,
    ) -> int:
        """Reconcile labels on agent-created PRs missing review labels.

        Called on startup to catch PRs where label addition failed due to
        orchestrator crash/restart or other failures.

        Args:
            code_review_label: The needs-code-review label to add
            code_reviewed_label: The code-reviewed label (skip if present)
            orchestrator_marker: Marker to identify orchestrator-created PRs
            is_pr_in_scope: Optional current-run scope predicate. When provided,
                out-of-scope PRs are not mutated.

        Returns:
            Number of PRs that were fixed
        """
        if not self.pr_tracker:
            logger.warning("[LABEL_SYNC] No PR tracker configured for reconciliation")
            return 0

        fixed_count = 0

        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.LABEL_SYNC_SCAN,
                scope=gh_audit.AuditScope.STARTUP,
            ):
                prs = self.pr_tracker.list_prs(state="open", limit=100)
        except Exception as e:
            logger.warning("[LABEL_SYNC] Failed to list PRs for reconciliation: %s", e)
            return 0

        for pr in prs:
            # Only reconcile PRs created by the orchestrator
            if orchestrator_marker not in pr.body:
                continue

            # Check if it already has a review label
            has_review_label = (
                code_review_label in pr.labels or
                (code_reviewed_label and code_reviewed_label in pr.labels)
            )
            if has_review_label:
                continue

            # Scope checks may fetch linked issues when filters are configured,
            # so run them only for PRs that would otherwise be mutated.
            if is_pr_in_scope is not None and not is_pr_in_scope(pr):
                logger.debug("[LABEL_SYNC] Skipping out-of-scope orphaned PR #%d", pr.number)
                continue

            # Add the needs-code-review label
            try:
                self.labels.add_label(pr.number, code_review_label)
                fixed_count += 1
                logger.info("[LABEL_SYNC] Added '%s' to orphaned PR #%d", code_review_label, pr.number)
            except Exception as e:
                logger.warning("[LABEL_SYNC] Failed to reconcile label on PR #%d: %s", pr.number, e)
                self.events.publish(
                    make_trace_event(
                        EventName.APPLY_FAILED,
                        {
                            "step_type": "label_sync_reconcile",
                            "pr_number": pr.number,
                            "error": str(e),
                        },
                    )
                )

        if fixed_count > 0:
            logger.info("Reconciled labels on %d orphaned PR(s)", fixed_count)

        return fixed_count
