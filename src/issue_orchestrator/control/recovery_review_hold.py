"""Queued reviews the validated-work recovery owner holds (#7455).

On a normal publish the aggregate recovery owner
(:class:`~.aggregate_recovery_block.AggregateRecoveryBlocks`) holds the issue
behind ``recovery-pending`` until it has routed the published PR to review, and
only then releases the hold. A review queued inside that window is not stale:
its issue is held by an owner that will release it. So it waits on its queue:

* at PLANNING time, from the owner's local record (no GitHub read), so a held
  review neither takes a launch slot nor spends live reads every tick while
  other reviews wait;
* at LAUNCH time, from the live labels (``refuse_unlaunchable_review``), for a
  hold the plan did not see yet.

Any other block on the issue withdraws the review as before.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .session_launch_types import REVIEW_HELD_BY_RECOVERY

if TYPE_CHECKING:
    from ..domain.models import PendingReview
    from .planner_types import SkippedItem

logger = logging.getLogger(__name__)


class RecoveryHolds(Protocol):
    def holds_recovery(self, issue_number: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class _NoRecoveryHolds:
    """For compositions without validated-work recovery (never production)."""

    def holds_recovery(self, issue_number: int) -> bool:
        del issue_number
        return False


NO_RECOVERY_HOLDS: RecoveryHolds = _NoRecoveryHolds()


def recovery_held_reviews(
    pending: Iterable["PendingReview"], holds: RecoveryHolds
) -> frozenset[int]:
    """PR numbers of the queued reviews whose issue the recovery owner holds.

    A store that cannot be read holds nothing here: the launch-time check reads
    the live labels and still makes the review wait if the hold is real.
    """
    held: set[int] = set()
    for review in pending:
        try:
            if holds.holds_recovery(review.issue_number):
                held.add(review.pr_number)
        except Exception as error:  # store-defined read failure
            logger.warning(
                "[REVIEW] Could not read the recovery hold for issue #%d; the launch-time check decides: %s",
                review.issue_number,
                error,
            )
    return frozenset(held)


def withhold_recovery_held(
    pending: Iterable["PendingReview"], held: frozenset[int], skipped: list["SkippedItem"]
) -> list["PendingReview"]:
    """The queued reviews planning may launch; each held one is reported waiting."""
    from .planner_types import SkippedItem

    launchable: list["PendingReview"] = []
    for review in pending:
        if review.pr_number in held:
            skipped.append(
                SkippedItem(item_type="review", number=review.pr_number, reason=REVIEW_HELD_BY_RECOVERY)
            )
            continue
        launchable.append(review)
    return launchable


__all__ = [
    "NO_RECOVERY_HOLDS",
    "RecoveryHolds",
    "recovery_held_reviews",
    "withhold_recovery_held",
]
