"""Pull request tracker port for PR operations.

This module defines the protocol (interface) for pull request tracking operations
and the PRInfo data class for representing pull request data.

Naming: "Tracker" implies external system CRUD operations, not internal storage.
This is an execution-layer interface.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol


class ReviewState(Enum):
    """GitHub PR review states."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"
    PENDING = "PENDING"


# GitHub `statusCheckRollup.state` values (uppercase, as returned by GraphQL).
# Used to disambiguate `mergeable_state == "unstable" | "blocked"` between
# "checks still running" (PENDING/EXPECTED) and "a check actually failed"
# (FAILURE/ERROR). None means we did not fetch / repository has no checks.
StatusCheckRollupState = Literal[
    "SUCCESS", "FAILURE", "PENDING", "EXPECTED", "ERROR"
]


# Capability outcome of a status-check-rollup read. Distinguishes a token
# that *cannot* read check status from a repo that *has* no checks, so the
# reconciler never hides a missing permission behind a "no checks" default.
StatusCheckRollupCapability = Literal["ok", "permission_denied", "transient_error"]


@dataclass(frozen=True)
class StatusCheckRollupRead:
    """Typed outcome of reading a PR head-commit status-check rollup.

    The awaiting-merge reconciler must treat three cases differently, so
    this read never collapses them into a bare ``state | None``:

    - ``ok``: the read succeeded. ``state`` is the rollup state, or
      ``None`` when the PR/commit genuinely has no checks configured.
    - ``permission_denied``: the configured GitHub token lacks the
      capability to read check status (e.g. a fine-grained PAT missing
      the Checks / commit-status read scope). This is a persistent
      operator problem, not a transient blip — callers surface it loudly
      and bound retries instead of silently defaulting to "no checks".
    - ``transient_error``: an ordinary GitHub failure (5xx, timeout).
      Safe to retry next tick; callers treat it as "rollup not yet
      known" (PENDING-equivalent) rather than escalating.
    """

    state: StatusCheckRollupState | None
    capability: StatusCheckRollupCapability = "ok"

    @property
    def permission_denied(self) -> bool:
        return self.capability == "permission_denied"


@dataclass
class PRInfo:
    """Information about a pull request.

    Attributes:
        number: The PR number.
        title: The PR title.
        url: The URL to view the PR.
        branch: The head branch name (source branch).
        body: The PR description/body text.
        state: The PR state - "open", "closed", or "merged".
        labels: List of label names on the PR.
        draft: Whether the PR is in draft state.
        mergeable_state: GitHub's `mergeable_state` (clean/dirty/behind/unstable/
            blocked/...). Says merge readiness.
        status_check_rollup: Aggregated state of required + non-required checks
            on the PR head commit. Says check truth. `None` when not fetched
            (only the single-PR `get_pr` path populates this).
    """

    number: int
    title: str
    url: str
    branch: str
    body: str
    state: str  # "open", "closed", "merged"
    labels: list[str]
    draft: bool | None = None
    mergeable_state: str | None = None
    status_check_rollup: StatusCheckRollupState | None = None

    @property
    def is_closed_unmerged(self) -> bool:
        """True when the PR is closed *without* having been merged.

        This is the sole precondition for applying ``blocked:pr-closed``.
        GitHub's raw REST ``state`` field is only ``"open"``/``"closed"`` — a
        merged PR is reported as ``"closed"``. Adapters normalize a merged PR's
        state to ``"merged"`` (from ``merged``/``merged_at``), so a ``"closed"``
        state here genuinely means closed without a merge — a merged or
        still-open PR is never mistaken for one.
        """
        return (self.state or "").strip().lower() == "closed"


@dataclass(frozen=True)
class PRRef:
    """Lightweight pull-request reference sourced directly from a search result.

    Carries only the fields GitHub's ``/search/issues`` response provides
    without a per-PR fetch: ``number``, ``url``, ``title``, ``body``. Use this
    for cheap "which PR is this" lookups — e.g. matching the orchestrator body
    marker — where a full :class:`PRInfo` is not needed. Resolving a list of
    refs costs one search call regardless of how many PRs match, versus the
    one-fetch-per-candidate that :meth:`PullRequestTracker.get_prs_for_issue`
    pays to hydrate full ``PRInfo`` objects.

    The head ``branch``, ``mergeable_state``, and check-rollup of a full
    ``PRInfo`` are intentionally absent here — needing them is the signal to
    use ``get_prs_for_issue``/``get_pr`` instead.
    """

    number: int
    url: str
    title: str
    body: str


class PullRequestTracker(Protocol):
    """Protocol for pull request tracking operations.

    This protocol defines the interface for creating, retrieving, and managing
    pull requests. Implementations can use different platforms (GitHub API,
    GitLab API, etc.) while maintaining the same interface.

    Naming: "Tracker" (not "Repository") because this represents an external
    platform's API, not internal persistence.
    """

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified head branch.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get all pull requests associated with a specific issue.

        Finds PRs where:
        - Branch starts with the issue number followed by a dash (e.g., "328-feature-name")
        - OR title contains "#issue_number" (e.g., "#328: Feature")

        Args:
            issue_number: The issue number to find PRs for.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs associated with the issue.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def search_pr_refs_for_issue(self, issue_number: int) -> list["PRRef"]:
        """Return lightweight PR references for an issue using a single search.

        Same association rule as :meth:`get_prs_for_issue` (head branch starts
        with the issue number, or the PR references ``#issue_number``), but does
        NOT hydrate each candidate with a per-PR ``GET`` — it maps the search
        result items directly to :class:`PRRef`. This is one GitHub call
        regardless of how many PRs match, intended for body/marker matching
        where the full ``PRInfo`` (head branch, mergeable state, check rollup)
        is not needed. Returns PRs in any state.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Args:
            label: The label name to filter by.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified label.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        Note: ``status_check_rollup`` is not populated by this method.
        Callers that need check-status visibility (the awaiting-merge
        post-publish classifier) should use
        ``read_pr_status_check_rollup`` instead — that variant pays an
        extra GraphQL round-trip per call and classifies failures.

        Args:
            pr_number: The PR number to retrieve.

        Returns:
            The PRInfo object if found, None otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def read_pr_status_check_rollup(self, pr_number: int) -> "StatusCheckRollupRead":
        """Read a PR's head-commit status-check rollup, classifying failures.

        Costs one GraphQL round-trip. Call this ONLY when the rollup is
        decisive for a merge-readiness decision — the awaiting-merge
        reconciler fetches it solely for reviewer-approved PRs whose
        ``mergeable_state`` is ``unstable`` or ``blocked``. Terminal
        (closed/merged) and ``clean``/``dirty``/``behind`` PRs must NOT
        call this; they pay no rollup cost.

        Unlike a plain PR fetch, this never swallows a permission failure
        into a ``None`` rollup. The returned ``StatusCheckRollupRead``
        distinguishes a token that cannot read check status
        (``permission_denied``) from a repo that has no checks
        (``ok`` with ``state=None``) and from a transient GitHub failure
        (``transient_error``).

        Returns:
            A :class:`StatusCheckRollupRead` describing the rollup state
            and the read capability.
        """
        ...

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List pull requests.

        Args:
            state: Filter by PR state ("open", "closed", "merged", or "all").
                  Defaults to "open".
            limit: Maximum number of PRs to return. Defaults to 100.

        Returns:
            A list of PRInfo objects.
            Returns empty list if no PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None
    ) -> PRInfo:
        """Create a new pull request.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch). Defaults to "main".
            draft: Whether to create the PR as a draft. Defaults to None (provider default).

        Returns:
            A PRInfo object representing the newly created PR.

        Raises:
            RepositoryError: If there's an error creating the PR.
            BranchNotFoundError: If the head or base branch doesn't exist.
            ValidationError: If the title or body is invalid.
        """
        ...

    def set_pr_draft(self, pr_number: int, draft: bool) -> None:
        """Set draft status on a pull request.

        Args:
            pr_number: The PR number to update.
            draft: True to mark as draft, False to mark ready for review.
        """
        ...

    def close_pr(self, pr_number: int) -> None:
        """Close a pull request.

        Args:
            pr_number: The PR number to close.

        Raises:
            RepositoryError: If there's an error closing the PR.
        """
        ...

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment to an issue or pull request.

        Args:
            issue_or_pr_number: The issue or PR number to comment on.
            body: The comment text to add.

        Returns:
            The URL of the created comment.

        Raises:
            RepositoryError: If there's an error adding the comment.
            IssueNotFoundError: If the issue/PR doesn't exist.
            ValidationError: If the comment body is empty or invalid.
        """
        ...

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get all reviews on a pull request.

        Args:
            pr_number: The PR number to get reviews for.

        Returns:
            List of review dicts with 'state', 'body', 'user' etc.
            Returns empty list if no reviews found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...


# Backwards compatibility alias
PRRepository = PullRequestTracker
