"""External state reconciliation module.

This module implements:
1. Optimistic concurrency control for GitHub state mutations (pre-mutation checks)
2. Startup reconciliation to discover and fix discrepancies between local and remote state

Before any mutation (labels, PR creation, comments), the system must:
1. Fetch current external snapshot
2. Compare against expected prior state
3. If mismatch, abort and raise ReconciliationRequired
4. If match, proceed with mutation

This prevents race conditions with humans or other tools, stale transitions,
and partial or contradictory state updates.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, FrozenSet

logger = logging.getLogger(__name__)


class ReconciliationRequired(Exception):
    """Raised when external state doesn't match expected prior state.

    Before attempting any mutation, fetch current external state and compare
    against expected prior state. If mismatch, abort and raise this.

    Valid responses to this exception:
    - pause issue/session
    - mark needs-reconciliation
    - notify human or tech lead agent

    Invalid responses:
    - partially apply mutations
    - guess intent
    - overwrite external state
    """

    def __init__(
        self,
        entity_type: str,
        entity_id: int,
        expected: "ExternalSnapshot",
        actual: "ExternalSnapshot",
        reason: str = "",
    ):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected = expected
        self.actual = actual
        self.reason = reason

        msg = f"{entity_type} #{entity_id}: state mismatch."
        if reason:
            msg += f" {reason}"
        msg += f" Expected labels {expected.labels}, found {actual.labels}"

        super().__init__(msg)


@dataclass(frozen=True)
class ExternalSnapshot:
    """Immutable snapshot of external state for an issue or PR.

    This captures the state that must be verified before any mutation.
    """

    # Issue/PR number
    number: int

    # Current labels on the issue/PR
    labels: FrozenSet[str] = field(default_factory=frozenset)

    # PR state if applicable (None for issues without PR)
    pr_state: Optional[str] = None  # "open", "closed", "merged", None

    # PR number if this is an issue with a linked PR
    linked_pr: Optional[int] = None

    @classmethod
    def for_issue(cls, number: int, labels: set[str]) -> "ExternalSnapshot":
        """Create snapshot for an issue."""
        return cls(number=number, labels=frozenset(labels))

    @classmethod
    def for_pr(
        cls,
        number: int,
        labels: set[str],
        state: str,
    ) -> "ExternalSnapshot":
        """Create snapshot for a PR."""
        return cls(
            number=number,
            labels=frozenset(labels),
            pr_state=state,
        )

    def labels_match(self, other: "ExternalSnapshot") -> bool:
        """Check if labels match between snapshots."""
        return self.labels == other.labels

    def contains_labels(self, required: set[str]) -> bool:
        """Check if snapshot contains all required labels."""
        return required.issubset(self.labels)

    def excludes_labels(self, forbidden: set[str]) -> bool:
        """Check if snapshot excludes all forbidden labels."""
        return not forbidden.intersection(self.labels)


@dataclass(frozen=True)
class ExpectedState:
    """Expected prior state for a transition.

    Before applying a mutation, verify the current state satisfies this.
    """

    # Labels that MUST be present
    required_labels: FrozenSet[str] = field(default_factory=frozenset)

    # Labels that MUST NOT be present
    forbidden_labels: FrozenSet[str] = field(default_factory=frozenset)

    # If set, PR must be in this state
    required_pr_state: Optional[str] = None  # "open", "closed", "merged"

    @classmethod
    def with_labels(
        cls,
        required: set[str] | None = None,
        forbidden: set[str] | None = None,
    ) -> "ExpectedState":
        """Create expected state with label requirements."""
        return cls(
            required_labels=frozenset(required or set()),
            forbidden_labels=frozenset(forbidden or set()),
        )

    def is_satisfied_by(self, snapshot: ExternalSnapshot) -> tuple[bool, str]:
        """Check if a snapshot satisfies this expected state.

        Returns:
            Tuple of (satisfied, reason). If not satisfied, reason explains why.
        """
        # Check required labels
        missing = self.required_labels - snapshot.labels
        if missing:
            return False, f"Missing required labels: {missing}"

        # Check forbidden labels
        present = self.forbidden_labels & snapshot.labels
        if present:
            return False, f"Has forbidden labels: {present}"

        # Check PR state if required
        if self.required_pr_state is not None:
            if snapshot.pr_state != self.required_pr_state:
                return False, (
                    f"PR state mismatch: expected {self.required_pr_state}, "
                    f"found {snapshot.pr_state}"
                )

        return True, ""


@dataclass
class ReconciliationResult:
    """Result of a reconciliation check."""

    # Whether the check passed
    passed: bool

    # The expected state
    expected: ExpectedState

    # The actual snapshot
    actual: ExternalSnapshot

    # Reason for failure (empty if passed)
    reason: str = ""

    @classmethod
    def success(
        cls,
        expected: ExpectedState,
        actual: ExternalSnapshot,
    ) -> "ReconciliationResult":
        """Create a successful result."""
        return cls(passed=True, expected=expected, actual=actual)

    @classmethod
    def failure(
        cls,
        expected: ExpectedState,
        actual: ExternalSnapshot,
        reason: str,
    ) -> "ReconciliationResult":
        """Create a failure result."""
        return cls(passed=False, expected=expected, actual=actual, reason=reason)


def check_reconciliation(
    expected: ExpectedState,
    actual: ExternalSnapshot,
    entity_type: str = "issue",
) -> ReconciliationResult:
    """Check if actual state satisfies expected state.

    Args:
        expected: What state we expect to see
        actual: What state we actually found
        entity_type: Type of entity for error messages

    Returns:
        ReconciliationResult indicating pass/fail
    """
    satisfied, reason = expected.is_satisfied_by(actual)

    if satisfied:
        logger.debug(
            "Reconciliation passed for %s #%d: labels=%s",
            entity_type, actual.number, actual.labels
        )
        return ReconciliationResult.success(expected, actual)
    else:
        logger.warning(
            "Reconciliation failed for %s #%d: %s",
            entity_type, actual.number, reason
        )
        return ReconciliationResult.failure(expected, actual, reason)


def require_reconciliation(
    expected: ExpectedState,
    actual: ExternalSnapshot,
    entity_type: str = "issue",
) -> None:
    """Check reconciliation and raise if failed.

    Args:
        expected: What state we expect to see
        actual: What state we actually found
        entity_type: Type of entity for error messages

    Raises:
        ReconciliationRequired: If actual state doesn't satisfy expected state
    """
    result = check_reconciliation(expected, actual, entity_type)

    if not result.passed:
        raise ReconciliationRequired(
            entity_type=entity_type,
            entity_id=actual.number,
            expected=ExternalSnapshot(
                number=actual.number,
                labels=expected.required_labels,
            ),
            actual=actual,
            reason=result.reason,
        )


# =============================================================================
# Pause Label (needs-reconcile)
# =============================================================================

# Default namespace prefix for orchestrator labels
DEFAULT_LABEL_PREFIX = "io"

# Base key for the pause label (rendered as {prefix}:needs-reconcile)
PAUSE_LABEL_KEY = "needs-reconcile"


def get_pause_label(prefix: str = DEFAULT_LABEL_PREFIX) -> str:
    """Get the fully-rendered pause label.

    Args:
        prefix: Label namespace prefix (default: "io")

    Returns:
        The pause label (e.g., "io:needs-reconcile")
    """
    return f"{prefix}:{PAUSE_LABEL_KEY}"


def build_expected_for_mutation(
    *,
    required: set[str] | None = None,
    forbidden: set[str] | None = None,
    prefix: str = DEFAULT_LABEL_PREFIX,
) -> ExpectedState:
    """Build ExpectedState for a mutating action.

    This helper ensures the pause label is always forbidden (fail-closed).

    Args:
        required: Labels that must be present
        forbidden: Additional labels that must not be present
        prefix: Label namespace prefix

    Returns:
        ExpectedState with pause label in forbidden set
    """
    pause_label = get_pause_label(prefix)
    all_forbidden = {pause_label}
    if forbidden:
        all_forbidden.update(forbidden)

    return ExpectedState.with_labels(
        required=required,
        forbidden=all_forbidden,
    )
