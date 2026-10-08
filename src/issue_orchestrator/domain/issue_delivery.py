"""Whether a publication delivers its issue whole or in part, and why (#7288, #8689).

A completion can say so itself: ``coding-done completed --partial`` claims a
partial delivery, and ``--finishes-issue`` declares the PR that finishes an
issue earlier PRs delivered in part. A completion that says neither is judged
against the issue's own history: when the latest merged PR that links the
issue only refs it, the issue is mid-delivery, and a receipt that merely
forgot ``--partial`` (a forced receipt after a halted review exchange, porchpin
#327) must not turn the next slice into a PR that closes it. Leaving an issue
open is recoverable; closing it with work still owed is not.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .pr_issue_reference import body_links_issue, declares_partial_delivery


class DeliveryBasis(Enum):
    """What decided the delivery."""

    CLAIMED_PARTIAL = "claimed_partial"  # the completion said --partial
    DECLARED_FINAL = "declared_final"  # the completion said --finishes-issue
    INFERRED_PARTIAL = "inferred_partial"  # unstated; a merged PR already refs the issue
    WHOLE = "whole"  # unstated; nothing merged delivered the issue in part


@dataclass(frozen=True)
class IssueDelivery:
    """The delivery a publication makes: ``partial`` decides ``Refs`` or ``Closes``.

    ``evidence_pr`` is the merged partial PR an inferred delivery rests on;
    only an inferred delivery has one.
    """

    basis: DeliveryBasis
    evidence_pr: int | None = None

    def __post_init__(self) -> None:
        if (self.basis is DeliveryBasis.INFERRED_PARTIAL) != (self.evidence_pr is not None):
            raise ValueError("only an inferred partial delivery names its evidence PR")

    @property
    def partial(self) -> bool:
        return self.basis in (DeliveryBasis.CLAIMED_PARTIAL, DeliveryBasis.INFERRED_PARTIAL)

    @property
    def finishes(self) -> bool:
        """The completion declared the PR that finishes the issue (``--finishes-issue``)."""
        return self.basis is DeliveryBasis.DECLARED_FINAL

    @property
    def inferred(self) -> bool:
        return self.basis is DeliveryBasis.INFERRED_PARTIAL

    def explanation(self, issue_number: int) -> str | None:
        """Why an unclaimed delivery is partial, for the PR body and the
        completion result; None for a delivery the completion stated itself."""
        if not self.inferred:
            return None
        return (
            f"Partial delivery (inferred): the completion did not declare --partial, "
            f"but merged PR #{self.evidence_pr} already delivered #{issue_number} in "
            f"part, so this PR refs the issue instead of closing it. If it finishes "
            f"#{issue_number}, complete with --finishes-issue, or close the issue "
            f"after the merge."
        )


def stated_delivery(*, partial_pr: bool, finishes_issue: bool) -> IssueDelivery | None:
    """The delivery a completion states itself, or None when it states none."""
    if partial_pr and finishes_issue:
        raise ValueError("a completion cannot both claim partial delivery and finish the issue")
    if partial_pr:
        return IssueDelivery(DeliveryBasis.CLAIMED_PARTIAL)
    if finishes_issue:
        return IssueDelivery(DeliveryBasis.DECLARED_FINAL)
    return None


@dataclass(frozen=True)
class MergedPullRequest:
    """A merged PR as the delivery history reads it: its body and merge time."""

    number: int
    body: str
    merged_at: datetime

    def __post_init__(self) -> None:
        if self.merged_at.tzinfo is None:
            raise ValueError(f"merged PR #{self.number} needs a timezone-aware merge time")


def delivery_from_history(
    issue_number: int,
    merged: Iterable[MergedPullRequest],
    *,
    repo_slug: str,
) -> IssueDelivery:
    """The delivery an unstated completion makes, from the issue's merged PRs.

    The most recently MERGED PR whose body links the issue decides (merge
    time, then PR number for a tie; PR numbers alone are opening order, not
    merge order): one that only refs it leaves the issue mid-delivery, so this
    publication is partial too; one that closed it means the issue was
    reopened as new work, a whole delivery. A merged PR that merely mentions
    the issue is not part of its delivery.
    """
    linked = [
        pr for pr in merged
        if body_links_issue(pr.body, (issue_number,), repo_slug=repo_slug)
    ]
    if not linked:
        return IssueDelivery(DeliveryBasis.WHOLE)
    latest = max(linked, key=lambda pr: (pr.merged_at, pr.number))
    if declares_partial_delivery(latest.body, issue_number, repo_slug=repo_slug):
        return IssueDelivery(DeliveryBasis.INFERRED_PARTIAL, evidence_pr=latest.number)
    return IssueDelivery(DeliveryBasis.WHOLE)
