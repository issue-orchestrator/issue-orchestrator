"""Port: the branch and PR operations integration-branch mode needs (#8144).

A component of :class:`~.repository_host.RepositoryHost` (like the issue, label
and PR trackers), named on its own so the integration owner depends on exactly
these operations. Every failure raises
:class:`~.repository_host.RepositoryHostError`; nothing here returns a default
for a read it could not make.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..domain.integration_branch import (
    BranchComparison,
    BranchMergeOutcome,
    MergedIntoBranchListing,
    OpenPullRequestRef,
)

if TYPE_CHECKING:
    from .pull_request_tracker import StatusCheckRollupRead


class IntegrationBranchHost(Protocol):
    """Branch refs, comparisons and PR merges on the managed repository."""

    def branch_head(self, branch: str) -> str | None:
        """The branch's head commit SHA; None when the branch does not exist."""
        ...

    def create_branch(self, branch: str, sha: str) -> None:
        """Create *branch* at *sha*; fails when it already exists."""
        ...

    def fast_forward_branch(self, branch: str, sha: str) -> None:
        """Move *branch* to *sha*; fails unless that is a fast-forward."""
        ...

    def compare_commits(self, base: str, head: str) -> BranchComparison:
        """How *head* (a branch or SHA) relates to *base* (``base...head``)."""
        ...

    def merge_branch(self, *, base: str, head: str, message: str) -> BranchMergeOutcome:
        """Merge *head* into the branch *base* server-side (a merge commit)."""
        ...

    def update_pull_request_branch(self, pr_number: int, *, expected_head_sha: str) -> None:
        """Merge the PR's base into its branch server-side, only from that head.

        Fails when the head moved or the merge conflicts.
        """
        ...

    def merge_head_onto(self, branch: str, *, tip_sha: str, head_sha: str, message: str) -> str:
        """Merge *head_sha* into *branch* only while *branch* is still at *tip_sha*.

        Atomic on the base: a merge commit (parents tip, head) is made with the
        head's tree - exact, because the head contains the tip - and *branch*
        is moved to it as a fast-forward from *tip_sha*, which GitHub refuses
        if anything else moved the branch first. GitHub then marks the PR whose
        head it is merged. Returns the merge commit SHA; fails if the head does
        not contain the tip or the branch moved.
        """
        ...

    def find_open_pull_request(self, *, head: str, base: str) -> OpenPullRequestRef | None:
        """The open PR from branch *head* into *base*, if there is one."""
        ...

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        """Replace the PR's description."""
        ...

    def read_commit_check_rollup(self, sha: str) -> "StatusCheckRollupRead":
        """The checks on exactly commit *sha* (not "the PR's current head").

        A merge judged by PR number could read the checks of a head pushed
        after io's read and merge the earlier head on them (#8144 review r3).
        """
        ...

    def merged_pull_requests_into(self, base: str) -> MergedIntoBranchListing:
        """The PRs merged into *base*, most recently updated first, paged up to
        a cap; the listing says whether it reached the last page."""
        ...


__all__ = ["IntegrationBranchHost"]
