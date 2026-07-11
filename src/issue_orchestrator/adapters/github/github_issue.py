"""GitHubIssue - concrete implementation of the Issue Protocol for GitHub.

This is an immutable snapshot of a GitHub issue. Key design:
1. frozen=True for immutability
2. eq=False so we can define custom equality based on .key
3. Uses tuple[str, ...] for labels (immutable sequence)
4. __eq__ and __hash__ based on .key for entity semantics
"""

from dataclasses import dataclass

from ...domain.issue_key import IssueKey, GitHubIssueKey, parse_external_id
from ...domain.models import _base_of, _is_blocking_label


@dataclass(frozen=True, eq=False)
class GitHubIssue:
    """Immutable snapshot of a GitHub issue.

    Implements the Issue Protocol with key-based equality.
    Two GitHubIssue instances with the same key are considered equal,
    even if their other fields differ (different snapshots in time).

    Attributes:
        number: GitHub issue number (backing-store handle)
        repo: Repository in owner/repo format
        title: Issue title
        labels: Tuple of label names (immutable)
        state: Issue state ('open' or 'closed')
        body: Issue body/description
        milestone: Milestone name
        milestone_number: Milestone number
        milestone_due_on: Milestone due date (ISO string)
        created_at: Issue creation timestamp (ISO string)
    """

    number: int
    repo: str
    title: str
    labels: tuple[str, ...] = ()
    state: str = "open"
    body: str | None = None
    milestone: str | None = None
    milestone_number: int | None = None
    milestone_due_on: str | None = None
    created_at: str | None = None

    # -------------------------------------------------------------------------
    # Identity (IssueKey)
    # -------------------------------------------------------------------------

    @property
    def key(self) -> IssueKey:
        """Stable identity for this issue.

        Uses external ID from title prefix (e.g., [M1-011]) if present,
        otherwise falls back to the issue number as a string.
        """
        parsed = parse_external_id(self.title)
        external_id = parsed.external_id or str(self.number)
        return GitHubIssueKey(repo=self.repo, external_id=external_id)

    def __eq__(self, other: object) -> bool:
        """Equality based on key only (entity semantics)."""
        if not isinstance(other, GitHubIssue):
            return NotImplemented
        return self.key == other.key

    def __hash__(self) -> int:
        """Hash based on key only (for dict/set compatibility)."""
        return hash(self.key)

    # -------------------------------------------------------------------------
    # Computed Properties (from Issue Protocol)
    # -------------------------------------------------------------------------

    @property
    def agent_type(self) -> str | None:
        """Extract agent type from labels (e.g., 'agent:developer')."""
        for label in self.labels:
            if label.startswith("agent:"):
                return label
        return None

    @property
    def priority(self) -> int:
        """Extract priority level (lower = higher priority).

        Returns:
            1 for priority:high
            2 for priority:medium
            3 for priority:low
            4 for no priority label
        """
        if "priority:high" in self.labels:
            return 1
        elif "priority:medium" in self.labels:
            return 2
        elif "priority:low" in self.labels:
            return 3
        return 4

    @property
    def is_blocked(self) -> bool:
        """Check if issue has any blocking label.

        Blocking labels: blocked, blocked-*, needs-human, failed (legacy).
        """
        return any(_is_blocking_label(l) for l in self.labels)

    @property
    def is_in_progress(self) -> bool:
        """Check if issue has the 'in-progress' label."""
        return any(_base_of(l) == "in-progress" for l in self.labels)

    @property
    def needs_human(self) -> bool:
        """Check if issue specifically needs human intervention.

        Subset of blocked - checks for: needs-human, blocked-needs-human.
        """
        return any(_base_of(l) in ("blocked-needs-human", "needs-human") for l in self.labels)

    # -------------------------------------------------------------------------
    # Convenience
    # -------------------------------------------------------------------------

    def __repr__(self) -> str:
        """Concise representation for debugging."""
        return f"GitHubIssue(#{self.number}, key={self.key})"
