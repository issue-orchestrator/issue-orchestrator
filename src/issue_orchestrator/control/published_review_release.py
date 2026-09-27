"""Release the review of a PR that carries an issue's published validated work (#7293).

Validated-work recovery publishes a halted run's validated head and routes its
PR to review, but review discovery drops a PR whose issue is blocked. An issue
blocked only by the failed run's ``blocked-failed`` label, whose open PR still
carries that published work, needs its review RELEASED: not a failure
investigation, and never a reset that closes the PR.

This is the one owner of that transition. Both callers - the stuck sweep's
budgeted remedy (through ``ReleasePublishedReviewAction`` and the applier) and a
tech-lead ``reset_retry`` that the reset gate refused - get the same ordered,
revalidated writes and the same typed outcome:

1. custody is rechecked NOW: a PR closed since it was observed is the
   operator's abandonment, and the issue is left for an ordinary investigation;
2. the live labels are read: any block other than ``blocked-failed`` (a
   human's ``blocked``, needs-human, recovery-pending) has an owner of its own
   and is never lifted here;
3. ``pr-pending`` goes on, and must be confirmed before anything comes off -
   an issue with neither gate is exactly what launches a coder over the PR;
4. only then does ``blocked-failed`` come off, guarded on still being present
   and on no needs-human escalation having landed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ..domain.tech_lead_session import (
    PROPOSED_TECH_LEAD_LABEL,
    TECH_LEAD_OBSERVATION_LABEL,
    TechLeadSessionFlavor,
)
from ..ports.repository_host import RepositoryHostError
from ..infra.logging_config import issue_log
from .action_results import ActionResult
from .actions import AddLabelAction, ReleasePublishedReviewAction, RemoveLabelAction
from .published_review_custody import (
    PublishedReviewHold,
    PublishedReviewHolds,
    PublishedValidatedWorkHeld,
)
from .reconciliation import ReconciliationRequired, build_expected_for_mutation

if TYPE_CHECKING:
    from ..domain.models import PendingTechLeadReview
    from ..ports import Issue
    from .action_applier import ActionApplier
    from .actions import Action
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

_MACHINERY = frozenset(
    {PROPOSED_TECH_LEAD_LABEL.casefold(), TECH_LEAD_OBSERVATION_LABEL.casefold()}
)


def review_releasable(
    issue_labels: Sequence[str],
    holds: Sequence[PublishedReviewHold],
    label_manager: "LabelManager",
) -> bool:
    """The one eligibility rule for releasing a published PR's review.

    Shared by the sweep (choosing the remedy) and the owner (at apply time), so
    the two cannot disagree. Releasable only when ``blocked-failed`` is the
    issue's ONLY recoverable block AND a held PR carries no block of its own -
    review discovery also rejects a blocked PR, so lifting the issue block
    alone would report a release that never lets the review run.
    """
    blockers = {
        name.casefold()
        for name in label_manager.get_blocking(issue_labels)
        if name.casefold() not in _MACHINERY
    }
    unblocked_pr = any(not label_manager.get_blocking(hold.pr_labels) for hold in holds)
    return unblocked_pr and blockers == {label_manager.blocked_failed.casefold()}


class ReviewReleaseStatus(StrEnum):
    RELEASED = "released"
    #: No open PR carries published work any more; nothing was written.
    NOT_HELD = "not_held"
    #: Another block owns the issue, or blocked-failed is already gone.
    NOT_RELEASABLE = "not_releasable"
    #: pr-pending could not be put on; nothing was removed.
    GATE_FAILED = "gate_failed"
    #: The PR's review label could not be restored; nothing was removed.
    ROUTE_FAILED = "route_failed"
    #: pr-pending is on, but blocked-failed would not come off.
    BLOCK_REMOVAL_FAILED = "block_removal_failed"
    #: pr-pending is on, but just before the block would come off the release
    #: was no longer eligible (another block landed, the PR moved): nothing
    #: was removed.
    WITHDRAWN = "withdrawn"


_LEFT_ALONE = frozenset({ReviewReleaseStatus.NOT_HELD, ReviewReleaseStatus.NOT_RELEASABLE})
_PUBLISHED_WORK_REFUSAL = PublishedValidatedWorkHeld.STALE_REASON


@dataclass(frozen=True, slots=True)
class ReviewReleaseOutcome:
    issue_number: int
    status: ReviewReleaseStatus
    holds: tuple[PublishedReviewHold, ...]
    detail: str

    @property
    def released(self) -> bool:
        return self.status is ReviewReleaseStatus.RELEASED

    @property
    def left_alone(self) -> bool:
        """Nothing was written because nothing was the owner's to release."""
        return self.status in _LEFT_ALONE


@dataclass(frozen=True, slots=True)
class ReviewReleaseWrites:
    """The ordered, guarded writes that release one PR's review (steps 3-4).

    Shared by every release so each gets the same order and guards: the sweep
    and a refused reset (for published work, below) and the tech lead's
    ``release_withheld_review`` (#7399), which decide eligibility their own way
    and then hand this the issue and the PR to release.
    """

    labels: "LabelManager"
    apply: Callable[["Action"], ActionResult]
    #: The PR label review discovery scans for; empty when none is configured.
    review_label: str

    def release(
        self, issue_number: int, pr_number: int, reason: str, *,
        still_releasable: Callable[[], str | None],
    ) -> tuple["ReviewReleaseStatus", str]:
        """Release; ``reason`` is recorded on every write and returned on success.

        ``still_releasable`` is the caller's own eligibility rule, asked again
        immediately before the block comes off: the gate and route writes take
        time, and a block that lands meanwhile (a human's ``blocked``,
        recovery-pending, a PR block) has an owner of its own. It returns why
        the release no longer holds, or ``None`` (#7399 review r3).
        """
        gate = self.apply(AddLabelAction(
            issue_number=issue_number, label=self.labels.pr_pending, fresh_presence=True,
            reason=reason))
        if not gate.success:
            return ReviewReleaseStatus.GATE_FAILED, f"pr-pending not added: {gate.error}"
        routed = self._route(pr_number, reason)
        if routed is not None and not routed.success:
            return ReviewReleaseStatus.ROUTE_FAILED, f"review label not restored: {routed.error}"
        withdrawn = still_releasable()
        if withdrawn is not None:
            return ReviewReleaseStatus.WITHDRAWN, f"blocked-failed kept: {withdrawn}"
        try:
            lifted = self._lift(issue_number, reason)
        except ReconciliationRequired as refused:
            # The board moved (pr-pending gone, needs-human landed): not released.
            return ReviewReleaseStatus.BLOCK_REMOVAL_FAILED, f"blocked-failed kept: {refused}"
        if not lifted.success:
            return ReviewReleaseStatus.BLOCK_REMOVAL_FAILED, f"blocked-failed not removed: {lifted.error}"
        return ReviewReleaseStatus.RELEASED, reason

    def _route(self, pr_number: int, reason: str) -> ActionResult | None:
        """Make sure review discovery will find the PR: it scans the review label.

        A released issue whose PR lost that label has no route to review, so
        the label is restored on the PR before the issue block comes off.
        Nothing to do when no review label is configured.
        """
        if not self.review_label:
            return None
        return self.apply(AddLabelAction(
            issue_number=pr_number, label=self.review_label, fresh_presence=True,
            reason=reason))

    def _lift(self, issue_number: int, reason: str) -> ActionResult:
        return self.apply(RemoveLabelAction(
            issue_number=issue_number, label=self.labels.blocked_failed,
            reason=reason,
            expected=build_expected_for_mutation(
                required={self.labels.blocked_failed, self.labels.pr_pending},
                forbidden={self.labels.needs_human})))


@dataclass(frozen=True, slots=True)
class PublishedReviewRelease:
    custody: PublishedReviewHolds
    labels: "LabelManager"
    read_labels: Callable[[int], list[str]]
    apply: Callable[["Action"], ActionResult]
    #: The PR label review discovery scans for; empty when none is configured.
    review_label: str

    def release(self, issue_number: int) -> ReviewReleaseOutcome:
        holds, refusal = self._eligibility(issue_number)
        if refusal is not None:
            return self._outcome(issue_number, refusal[0], holds, refusal[1])
        described = "; ".join(hold.describe() for hold in holds)
        target = next(hold for hold in holds if not self.labels.get_blocking(hold.pr_labels))
        status, detail = self.writes.release(
            issue_number, target.pr_number, f"published validated work is under review: {described}",
            still_releasable=lambda: self._withdrawn(issue_number))
        return self._outcome(issue_number, status, holds, detail)

    def _eligibility(
        self, issue_number: int
    ) -> tuple[tuple[PublishedReviewHold, ...], tuple[ReviewReleaseStatus, str] | None]:
        """Custody and the eligibility rule, read now: the holds, and why not."""
        holds = self.custody.holds(issue_number)
        if not holds:
            return holds, (ReviewReleaseStatus.NOT_HELD, "no open PR carries published validated work")
        current = self.read_labels(issue_number)
        releasable = review_releasable(current, holds, self.labels)
        if not releasable:
            return holds, (ReviewReleaseStatus.NOT_RELEASABLE,
                           f"issue blocks {self.labels.get_blocking(current)}; a held PR carries "
                           f"its own block, or the issue's block is not only blocked-failed")
        return holds, None

    def _withdrawn(self, issue_number: int) -> str | None:
        _holds, refusal = self._eligibility(issue_number)
        return None if refusal is None else refusal[1]

    @property
    def writes(self) -> "ReviewReleaseWrites":
        return ReviewReleaseWrites(labels=self.labels, apply=self.apply, review_label=self.review_label)

    @staticmethod
    def _outcome(issue_number: int, status: ReviewReleaseStatus,
                 holds: tuple[PublishedReviewHold, ...], detail: str) -> ReviewReleaseOutcome:
        logger.info(issue_log(issue_number, "Published review release: %s (%s) (#7293)"),
                    status.value, detail)
        return ReviewReleaseOutcome(issue_number, status, holds, detail)


def published_review_release_for(applier: "ActionApplier", review_label: str) -> PublishedReviewRelease:
    """The owner over the applier's custody, labels and guarded label writes."""
    if applier.label_manager is None or applier.repository_host is None:
        raise RuntimeError("published review release requires labels and a repository host")
    return PublishedReviewRelease(
        custody=applier.runtime_lifecycle.published_review,
        labels=applier.label_manager,
        read_labels=applier.repository_host.get_issue_labels_fresh,
        apply=applier.apply,
        review_label=review_label,
    )


def apply_release_published_review(action: "Action", applier: "ActionApplier") -> ActionResult:
    """The applier handler: map the owner's typed outcome onto an ActionResult."""
    assert isinstance(action, ReleasePublishedReviewAction)
    outcome = published_review_release_for(applier, action.code_review_label).release(action.issue_number)
    number, status = action.issue_number, outcome.status.value
    if outcome.released:
        return ActionResult.ok(action, issue_number=number, status=status)
    refuse = ActionResult.skip if outcome.left_alone else ActionResult.fail
    return refuse(action, outcome.detail, issue_number=number, status=status)


def build_stuck_sweep_review_release_actions(
    issue_numbers: "tuple[int, ...]", code_review_label: str
) -> "list[Action]":
    """The sweep's budgeted remedy for each held issue: one owner command each."""
    return [
        ReleasePublishedReviewAction(issue_number=number, code_review_label=code_review_label)
        for number in issue_numbers
    ]


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


def refused_reset_disposition(
    refusal: str, issue_number: int, release: Callable[[int], ReviewReleaseOutcome]
) -> tuple[bool, dict[str, str]]:
    """Whether a refused tech-lead reset still settled the issue, and how.

    Only the published-work refusal names an owner that can make progress: the
    PR's review. The refusal then releases that review through this module's
    owner, and the investigation is satisfied only if the release happened -
    never on the refusal alone (the sweep may be disabled, or the release may
    fail, and then the issue is still stranded).
    """
    if refusal != _PUBLISHED_WORK_REFUSAL:
        return False, {}
    outcome = release(issue_number)
    return outcome.released, {"review_release": outcome.status.value}


def held_investigation_subjects(
    pending: Sequence["PendingTechLeadReview"], custody: PublishedReviewHolds
) -> frozenset[int]:
    """Queued failure investigations whose subject a published PR now owns.

    A failure investigation queued before recovery published the issue's work
    must not run over it (#7293): its likely remedies are an escalation to
    needs-human or a reset, and the review is what the issue actually needs.
    Launch-time revalidation withdraws these runs; the stuck sweep then finds
    the issue unowned and releases its review. An unreadable custody answer
    keeps the run - every destructive remedy it could reach rechecks custody
    and fails closed on its own.
    """
    held: set[int] = set()
    for item in pending:
        investigation = item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
        try:
            owned = investigation and bool(custody.holds(item.issue_number))
        except RepositoryHostError as error:
            logger.warning(issue_log(item.issue_number,
                "Published-review custody unreadable; keeping the queued investigation: %s"), error)
            continue
        held.update({item.issue_number} if owned else set())
    return frozenset(held)
