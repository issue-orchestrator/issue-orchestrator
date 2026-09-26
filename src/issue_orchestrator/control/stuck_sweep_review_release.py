"""The stuck sweep's policy for a blocked issue whose published work is under review (#7293).

Validated-work recovery publishes a halted run's validated head and routes its
PR to review, but review discovery drops a PR whose issue is blocked. So an
issue blocked only by the failed run's ``blocked-failed`` label, whose open PR
carries that published work, needs its review RELEASED - not a failure
investigation, and never a reset. Any other block (a human's ``blocked``,
needs-human) has an owner of its own; the sweep leaves it held and untouched.
``stuck_sweep`` decides when (on its budget); this module says what.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .published_review_custody import PublishedReviewHold

if TYPE_CHECKING:
    from ..ports import Issue
    from .actions import Action
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


def only_failure_blocked(
    issue: "Issue", label_manager: "LabelManager", machinery_folded: frozenset[str]
) -> bool:
    """Whether ``blocked-failed`` is the issue's ONLY recoverable block.

    That label records a failed run; with the run's validated work already
    published under an open PR it is the one block this sweep may lift, to let
    the review proceed. Any other block (needs-human, a human's ``blocked``)
    has an owner of its own and is never lifted here.
    """
    blockers = {
        name.casefold()
        for name in label_manager.get_blocking(issue.labels)
        if name.casefold() not in machinery_folded
    }
    return blockers == {label_manager.blocked_failed.casefold()}


def log_held_for_review(
    issue: "Issue", blocking_label: str, holds: tuple[PublishedReviewHold, ...]
) -> None:
    logger.info(
        "[STUCK_SWEEP] issue #%d (label=%s) is not stuck: %s; its review owns "
        "it, so it is neither investigated nor escalated (#7293)",
        issue.number,
        blocking_label,
        "; ".join(hold.describe() for hold in holds),
    )


def build_stuck_sweep_review_release_actions(
    issue_numbers: "tuple[int, ...]",
    label_manager: "LabelManager",
) -> "list[Action]":
    """Release a published PR's review by lifting the stale failure block (#7293).

    One label transition per issue: pr-pending goes on (the adds precede the
    removals) so the scheduler never sees the issue unblocked without it, and
    ``blocked-failed`` comes off - but only while it is still the block the
    sweep saw and no needs-human escalation has landed since.
    """
    from .actions import SyncLabelsAction
    from .reconciliation import build_expected_for_mutation

    return [
        SyncLabelsAction(
            issue_number=issue_number,
            add_labels=(label_manager.pr_pending,),
            remove_labels=(label_manager.blocked_failed,),
            reason="stuck sweep: published validated work is under review (#7293)",
            expected=build_expected_for_mutation(
                required={label_manager.blocked_failed},
                forbidden={label_manager.needs_human},
            ),
        )
        for issue_number in issue_numbers
    ]
