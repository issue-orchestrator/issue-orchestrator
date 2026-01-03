"""LabelProjection - pure logic for determining desired labels.

This module determines what labels an issue SHOULD have based on its state.
It contains no IO - just pure functions that map state to labels.

The projection is the single source of truth for label policy.

Usage:
    projection = LabelProjection(config)
    desired = projection.for_issue_state(IssueState.IN_PROGRESS)
    desired = projection.for_issue_with_status(issue_number, status, blocked_reason)
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set, FrozenSet

from ..infra.config import Config
from ..domain.state_machines.issue_machine import IssueState

logger = logging.getLogger(__name__)


class LabelCategory(Enum):
    """Categories of labels managed by the orchestrator."""

    STATUS = "status"  # in-progress, needs-review, etc.
    BLOCKED = "blocked"  # blocked-*, blocked-needs-human
    REWORK = "rework"  # needs-rework, rework-cycle-N
    REVIEW = "review"  # needs-code-review
    PRIORITY = "priority"  # priority:high, priority:low


@dataclass(frozen=True)
class DesiredLabels:
    """The set of labels an issue should have.

    This is an immutable snapshot of desired label state.
    """

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


class LabelProjection:
    """Determines what labels an issue should have based on state.

    This is a pure (stateless) service that implements label policy.
    All the rules for "what labels mean what" live here.
    """

    # Standard labels used by the orchestrator
    LABEL_IN_PROGRESS = "in-progress"
    LABEL_BLOCKED = "blocked"
    LABEL_BLOCKED_NEEDS_HUMAN = "blocked-needs-human"
    LABEL_NEEDS_CODE_REVIEW = "needs-code-review"
    LABEL_NEEDS_REWORK = "needs-rework"

    def __init__(self, config: Config):
        """Initialize with configuration.

        Args:
            config: Configuration with label settings
        """
        self.config = config

    def get_in_progress_label(self) -> str:
        """Get the in-progress label name (may be configurable)."""
        return self.config.get_label_in_progress()

    def for_issue_state(self, state: IssueState) -> DesiredLabels:
        """Get desired labels for an issue state.

        This is the core projection: state → labels.

        Args:
            state: The current IssueState

        Returns:
            DesiredLabels indicating what labels should be added/removed
        """
        in_progress = self.get_in_progress_label()

        if state == IssueState.AVAILABLE:
            # Available issues should not have any status labels
            return DesiredLabels.remove(in_progress)

        elif state == IssueState.CLAIMED:
            # Claimed issues get in-progress label
            return DesiredLabels.add(in_progress)

        elif state == IssueState.IN_PROGRESS:
            # In-progress issues keep the in-progress label
            return DesiredLabels.add(in_progress)

        elif state == IssueState.BLOCKED:
            # Blocked issues keep in-progress and get blocked label
            return DesiredLabels.add(in_progress, self.LABEL_BLOCKED)

        elif state == IssueState.NEEDS_HUMAN:
            # Needs-human gets the specific blocked label
            return DesiredLabels.add(in_progress, self.LABEL_BLOCKED_NEEDS_HUMAN)

        elif state == IssueState.PR_PENDING:
            # PR pending keeps in-progress (work is done, awaiting merge)
            return DesiredLabels.add(in_progress)

        elif state == IssueState.COMPLETED:
            # Completed issues should not have in-progress
            return DesiredLabels.remove(in_progress)

        else:
            logger.warning(f"Unknown issue state: {state}")
            return DesiredLabels()

    def for_blocked(self, reason: Optional[str] = None) -> DesiredLabels:
        """Get labels for a blocked issue.

        Args:
            reason: Optional reason code for the block

        Returns:
            DesiredLabels with the appropriate blocked label
        """
        in_progress = self.get_in_progress_label()
        if reason:
            blocked_label = f"blocked-{reason}"
        else:
            blocked_label = self.LABEL_BLOCKED
        return DesiredLabels.add(in_progress, blocked_label)

    def for_unblocked(self) -> DesiredLabels:
        """Get labels when transitioning from blocked to unblocked.

        Returns:
            DesiredLabels that removes all blocked-* labels
        """
        # We need to remove all blocked-* labels, but we don't know which ones
        # are present. The sync step will handle this by checking current labels.
        # Here we just indicate what prefixes to remove.
        return DesiredLabels(
            must_not_have=frozenset(["blocked"])  # Will match blocked-*
        )

    def for_review_needed(self, pr_number: int) -> DesiredLabels:
        """Get labels for a PR that needs review.

        Args:
            pr_number: The PR number

        Returns:
            DesiredLabels with needs-code-review label
        """
        return DesiredLabels.add(self.LABEL_NEEDS_CODE_REVIEW)

    def for_rework_needed(self, cycle: int = 1) -> DesiredLabels:
        """Get labels for an issue that needs rework.

        Args:
            cycle: The rework cycle number

        Returns:
            DesiredLabels with needs-rework and rework-cycle-N labels
        """
        cycle_label = f"rework-cycle-{cycle}"
        return DesiredLabels.add(self.LABEL_NEEDS_REWORK, cycle_label)

    def blocked_labels_pattern(self) -> str:
        """Get the pattern for blocked labels.

        Returns:
            Pattern string that matches all blocked labels
        """
        return "blocked"

    def rework_cycle_labels_pattern(self) -> str:
        """Get the pattern for rework cycle labels.

        Returns:
            Pattern string that matches rework-cycle-N labels
        """
        return "rework-cycle-"


def compute_label_changes(
    current: Set[str],
    desired: DesiredLabels,
) -> tuple[Set[str], Set[str]]:
    """Compute what label changes are needed.

    This is a pure function that computes the diff between current and desired.

    Args:
        current: Set of labels currently on the issue
        desired: DesiredLabels specifying what should change

    Returns:
        Tuple of (labels_to_add, labels_to_remove)
    """
    to_add: Set[str] = set(desired.to_add - current)
    to_remove: Set[str] = set(desired.to_remove & current)

    # Handle must_not_have patterns (prefix matching)
    for pattern in desired.must_not_have:
        for label in current:
            if label.startswith(pattern):
                to_remove.add(label)

    return to_add, to_remove
