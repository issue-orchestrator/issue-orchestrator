"""Shared validity checks for pending code reviews."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.blocked_open_pr import BlockedPRSkipReason

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager


class LabelledIssue(Protocol):
    """All review validity reads of an issue: its labels."""

    @property
    def labels(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class _IssueLabels:
    labels: tuple[str, ...]


@dataclass(frozen=True)
class ReviewValidity:
    """Whether a queued/discovered review is still valid to process."""

    valid: bool
    reason: str
    issue_labels: tuple[str, ...] = ()
    pr_labels: tuple[str, ...] = ()
    blocking_labels: tuple[str, ...] = ()

    @property
    def held_by_block(self) -> bool:
        """Rejected because a blocking label sits on the issue or the PR.

        Only ``issue_blocked`` and ``pr_blocked`` carry blocking labels; every
        other rejection (``pr_needs_rework``, ``review_label_missing``, ...) is
        not a block (#7294).
        """
        return bool(self.blocking_labels)


def evaluate_review_validity(
    *,
    config: "Config",
    label_manager: "LabelManager",
    issue: LabelledIssue | None,
    pr: "PRInfo | None" = None,
    review_label_confirmed: bool = False,
    review_admitted_blocks: frozenset[str] = frozenset(),
) -> ReviewValidity:
    """Return whether a review is still valid for queue/launch processing.

    ``review_admitted_blocks`` are casefolded issue blocks the review may run
    over, as decided by :mod:`.review_question_hold` (an agent's own open
    question, #7593). They still hold everything else.
    """
    issue_labels = tuple(issue.labels) if issue is not None else ()
    pr_labels = tuple(pr.labels) if pr is not None else ()

    if pr is None and issue is None:
        return ReviewValidity(
            valid=True,
            reason="ok",
        )

    if pr is not None:
        if pr.state.lower() != "open":
            return ReviewValidity(
                valid=False,
                reason="pr_not_open",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

        review_label_missing = (
            config.code_review_label
            and not review_label_confirmed
            and config.code_review_label not in pr.labels
        )
        if review_label_missing:
            return ReviewValidity(
                valid=False,
                reason="review_label_missing",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

        pr_blocking = tuple(label_manager.get_blocking(pr.labels))
        if pr_blocking:
            return ReviewValidity(
                valid=False,
                reason=BlockedPRSkipReason.PR_BLOCKED.value,
                issue_labels=issue_labels,
                pr_labels=pr_labels,
                blocking_labels=pr_blocking,
            )

        if label_manager.needs_rework in pr.labels:
            return ReviewValidity(
                valid=False,
                reason="pr_needs_rework",
                issue_labels=issue_labels,
                pr_labels=pr_labels,
            )

    if issue is None:
        return ReviewValidity(
            valid=True,
            reason="ok",
            pr_labels=pr_labels,
        )

    issue_blocking = tuple(
        label
        for label in label_manager.get_blocking(issue.labels)
        if label.casefold() not in review_admitted_blocks
    )
    if issue_blocking:
        return ReviewValidity(
            valid=False,
            reason=BlockedPRSkipReason.ISSUE_BLOCKED.value,
            issue_labels=issue_labels,
            pr_labels=pr_labels,
            blocking_labels=issue_blocking,
        )

    if label_manager.needs_rework in issue.labels:
        return ReviewValidity(
            valid=False,
            reason="issue_needs_rework",
            issue_labels=issue_labels,
            pr_labels=pr_labels,
        )

    return ReviewValidity(
        valid=True,
        reason="ok",
        issue_labels=issue_labels,
        pr_labels=pr_labels,
    )


@dataclass(frozen=True)
class ReviewWithholding:
    """This owner's answer twice: as the labels stand, and without one block.

    ``current`` is exactly what review discovery decides for the PR today;
    ``without_block`` is the same decision with ``block_label`` taken off the
    issue and nothing else changed. The review is withheld ONLY by that block
    when today's answer is ``issue_blocked`` and the answer without it admits
    the review (#7399) - no second copy of the validity rules is consulted.
    """

    current: ReviewValidity
    without_block: ReviewValidity
    block_label: str

    @property
    def withheld_only_by_block(self) -> bool:
        return (
            not self.current.valid
            and self.current.reason == BlockedPRSkipReason.ISSUE_BLOCKED
            and self.without_block.valid
        )


def evaluate_review_withholding(
    *,
    config: "Config",
    label_manager: "LabelManager",
    issue: LabelledIssue,
    pr: "PRInfo",
    block_label: str,
    review_admitted_blocks: frozenset[str] = frozenset(),
) -> ReviewWithholding:
    """Whether ``block_label`` on ``issue`` is all that keeps ``pr`` from review.

    The review label is NOT taken as confirmed: a PR that lost it is withheld
    by more than the block, because discovery would not list it at all.
    """
    folded = block_label.casefold()
    released = _IssueLabels(tuple(name for name in issue.labels if name.casefold() != folded))
    return ReviewWithholding(
        current=evaluate_review_validity(
            config=config, label_manager=label_manager, issue=issue, pr=pr,
            review_admitted_blocks=review_admitted_blocks,
        ),
        without_block=evaluate_review_validity(
            config=config, label_manager=label_manager, issue=released, pr=pr,
            review_admitted_blocks=review_admitted_blocks,
        ),
        block_label=block_label,
    )
