"""Issue Protocol - abstract interface for work items.

This defines the contract that any issue implementation must satisfy.
The core orchestrator logic depends only on this Protocol, not on
concrete implementations like GitHubIssue.

Key design decisions:
1. Issue is a Protocol (structural typing) - implementations don't need to inherit
2. Issue.key returns IssueKey for stable identity
3. Issue equality/hash should be based on .key only (entity semantics)
4. Issue is an immutable snapshot - fields may differ between snapshots of same issue
"""

from typing import Protocol, Sequence, runtime_checkable

from ..domain.issue_key import IssueKey


@runtime_checkable
class Issue(Protocol):
    """Protocol for work items.

    An Issue represents a snapshot of a work item at a point in time.
    Two snapshots of the same issue have the same .key but may differ
    in other fields (labels changed, etc.).

    Implementations must:
    - Be immutable (frozen dataclass recommended)
    - Define __eq__ and __hash__ based on .key only
    - Provide all required properties
    """

    @property
    def key(self) -> IssueKey:
        """Stable identity for this issue.

        Used for dict keys, set membership, and equality comparisons.
        Two issues with the same key are the same logical issue,
        even if their other fields differ (different snapshots).
        """
        ...

    @property
    def number(self) -> int:
        """Backing-store handle (e.g., GitHub issue number).

        Note: This is included for pragmatic migration. In a multi-platform
        future, this might become optional or be replaced by key.stable_id().
        """
        ...

    @property
    def title(self) -> str:
        """Issue title."""
        ...

    @property
    def labels(self) -> Sequence[str]:
        """Labels attached to this issue."""
        ...

    @property
    def body(self) -> str | None:
        """Issue body/description."""
        ...

    @property
    def state(self) -> str:
        """Issue state: 'open' or 'closed'."""
        ...

    @property
    def milestone(self) -> str | None:
        """Milestone name, if any."""
        ...

    @property
    def milestone_number(self) -> int | None:
        """Milestone number in the backing store, if any."""
        ...

    @property
    def milestone_due_on(self) -> str | None:
        """Milestone due date as ISO string, if any."""
        ...

    @property
    def created_at(self) -> str | None:
        """ISO-8601 creation timestamp from the backing store, if known.

        Crash-safe reconciliation input: the health-review trigger derives
        its last-fired time from the newest marker-labeled anchor's creation
        time when the durable store is behind (ADR-0031 §4).
        """
        ...

    @property
    def agent_type(self) -> str | None:
        """Agent type label (e.g., 'agent:developer'), if any."""
        ...

    @property
    def priority(self) -> int:
        """Priority level (lower = higher priority)."""
        ...

    @property
    def is_blocked(self) -> bool:
        """Whether this issue is blocked."""
        ...

    @property
    def is_in_progress(self) -> bool:
        """Whether this issue is in progress."""
        ...

    @property
    def needs_human(self) -> bool:
        """Whether this issue needs human intervention."""
        ...
