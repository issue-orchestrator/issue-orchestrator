"""Who holds an issue whose validated work is already published under an open PR (#7293).

Validated-work recovery publishes a halted or terminated run's validated head
and routes its PR to review; the record then rests RECOVERED ("published +
review routed"). From that moment the work lives in the PR, not in the store:
RECOVERED is a *resolved* state, so every gate that asks "is unresolved work at
risk?" answers no. That is correct for the store, and wrong for the issue:

* the stuck sweep saw a blocked issue with no reconciler owner and re-injected
  it as a failed run, investigated it, and after three cycles escalated it to
  needs-human, although the only thing it was waiting on was its review;
* a ``reset_retry`` then closed the PR and deleted its branch - destroying
  green, published, validated work - because a resolved record does not refuse
  a reset (porchpin#392).

This module is the one owner of the question those paths got wrong: *does an
open pull request for this issue carry its published validated work?* It is
answered from the durable validated-work record (never inferred from labels)
and one authoritative, uncached read of the open PRs on each published
record's branch, which is made only when a published record exists.

A PR carries the work when it is open, on the record's branch, and is either the
PR the work was published into or points at the exact published head. A later
push to that PR (a rework, a base update) does not release it: the validated
work is still underneath, and closing the PR or deleting its branch destroys it
just the same. Closing the PR is the operator's explicit abandonment; nothing
here ever infers one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.validated_work import ValidatedWorkState

if TYPE_CHECKING:
    from ..domain.validated_work_commands import (
        ValidatedWorkDisposition,
        ValidatedWorkDispositionBatch,
    )
    from ..ports.pull_request_tracker import PRInfo


class ValidatedWorkDispositionReader(Protocol):
    def for_issue(self, issue_number: int) -> "ValidatedWorkDispositionBatch": ...


class BranchPullRequestReader(Protocol):
    def get_open_prs_for_branch_complete(self, branch: str) -> list["PRInfo"]: ...


@dataclass(frozen=True, slots=True)
class PublishedReviewHold:
    """One open PR that carries an issue's published validated work."""

    issue_number: int
    pr_number: int
    branch_name: str
    record_id: str
    published_head_sha: str
    # The PR's own labels as read: a PR carrying its own block (a terminated
    # review's blocked-failed) is held by that block, not releasable (#7293).
    pr_labels: tuple[str, ...] = ()

    def describe(self) -> str:
        return (
            f"PR #{self.pr_number} ({self.branch_name}) carries published "
            f"validated work {self.published_head_sha[:12]}"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "branch_name": self.branch_name,
            "record_id": self.record_id,
            "published_head_sha": self.published_head_sha,
        }


class PublishedValidatedWorkHeld(RuntimeError):
    """A destructive issue operation was refused: an open PR carries published work."""

    STALE_REASON = "published_validated_work_under_review"

    def __init__(self, issue_number: int, holds: tuple[PublishedReviewHold, ...]) -> None:
        if not holds:
            raise ValueError("a published-work refusal must name the PR it protects")
        self.issue_number = issue_number
        self.holds = holds
        prs = ", ".join(f"#{hold.pr_number}" for hold in holds)
        super().__init__(
            f"issue #{issue_number} has published validated work under open PR "
            f"{prs}: {'; '.join(hold.describe() for hold in holds)}. Resetting "
            f"would close the PR and delete its branch. Retry the issue to "
            f"release its review, or close PR {prs} to abandon that work first."
        )

    def observation(self) -> dict[str, object]:
        return {
            "issue_number": self.issue_number,
            "holds": [hold.to_dict() for hold in self.holds],
        }


def published_review_holds(
    batch: "ValidatedWorkDispositionBatch", open_prs: Sequence["PRInfo"]
) -> tuple[PublishedReviewHold, ...]:
    """Pure matching of published records against the issue's open PRs."""
    holds: dict[int, PublishedReviewHold] = {}
    for record in _published(batch):
        for pr in open_prs:
            if pr.number in holds or not _carries(pr, record):
                continue
            assert record.published_head_sha is not None  # RECOVERED guarantees it
            holds[pr.number] = PublishedReviewHold(
                issue_number=batch.issue_number,
                pr_number=pr.number,
                branch_name=record.key.branch_name,
                record_id=record.record_id,
                published_head_sha=record.published_head_sha,
                pr_labels=tuple(pr.labels),
            )
    return tuple(holds[number] for number in sorted(holds))


def _published(
    batch: "ValidatedWorkDispositionBatch",
) -> tuple["ValidatedWorkDisposition", ...]:
    return tuple(
        record
        for record in batch.dispositions
        if record.state is ValidatedWorkState.RECOVERED
    )


def _carries(pr: "PRInfo", record: "ValidatedWorkDisposition") -> bool:
    if pr.branch != record.key.branch_name:
        return False
    return pr.number == record.pr_number or pr.head_sha == record.published_head_sha


@dataclass(frozen=True, slots=True)
class PublishedReviewCustody:
    """Answers, and enforces, "an open PR carries this issue's published work"."""

    work: ValidatedWorkDispositionReader
    pull_requests: BranchPullRequestReader

    def holds(self, issue_number: int) -> tuple[PublishedReviewHold, ...]:
        batch = self.work.for_issue(issue_number)
        published = _published(batch)
        if not published:
            # No published record: nothing to protect, and no GitHub read spent.
            return ()
        # Read by the records' own branches - uncached and complete, so a PR
        # cannot hide behind a capped issue search or a stale cache entry.
        branches = sorted({record.key.branch_name for record in published})
        return published_review_holds(batch, [
            pr for branch in branches
            for pr in self.pull_requests.get_open_prs_for_branch_complete(branch)
        ])

    def require_released(self, issue_number: int) -> None:
        """Refuse a destructive issue operation while an open PR holds its work."""
        holds = self.holds(issue_number)
        if holds:
            raise PublishedValidatedWorkHeld(issue_number, holds)


class PublishedReviewHolds(Protocol):
    def holds(self, issue_number: int) -> tuple[PublishedReviewHold, ...]: ...


@dataclass(frozen=True, slots=True)
class _NoPublishedReviewHolds:
    """For compositions without validated work (never the production engine)."""

    def holds(self, issue_number: int) -> tuple[PublishedReviewHold, ...]:
        return ()


NO_PUBLISHED_REVIEW_HOLDS: PublishedReviewHolds = _NoPublishedReviewHolds()


__all__ = [
    "BranchPullRequestReader",
    "NO_PUBLISHED_REVIEW_HOLDS",
    "PublishedReviewCustody",
    "PublishedReviewHold",
    "PublishedReviewHolds",
    "PublishedValidatedWorkHeld",
    "ValidatedWorkDispositionReader",
    "published_review_holds",
]
