"""Execution-time owner for the tech lead's ``release_withheld_review`` (#7399).

An issue can carry ``blocked-failed`` while its open, CI-green PR waits on a
code review that the block withholds: review discovery drops the PR with
``issue_blocked`` on every scan (porchpin#382, exam Case B). The stuck sweep
releases such a review itself only when the PR carries RECOVERED validated
work (#7293); every other shape reached a tech lead that could diagnose it but
only escalate. This action lets the tech lead release it.

The action is intent. Nothing the agent wrote is trusted: immediately before
any write the orchestrator re-verifies every precondition against the owner
that already answers it, and never re-derives one:

1. **no live session** — the issue-runtime owners the reset boundary probes
   (sessions, the persistent exchange pair, supervised exchange jobs, a
   pending publish retry); an unverifiable owner counts as live;
2. **no claim** — the durable pending-work ledger holds no claim on the
   issue, readable or not, except the proposing run's own (the investigation
   that proposed this holds one on its focus while it completes);
3. **no newer failure** — the session history owner has no failed session for
   the issue that it cannot place before the tech lead's observation;
4. **the issue is open**, read fresh;
5. **exactly one open PR** is the issue's, linked and branch-scoped as review
   discovery links it (one complete, uncached listing), then read fresh (the
   listing carries no labels) and admitted by review discovery's own per-PR
   gate (``PRScanner.review_admission``: configured scope, active branch) as
   it is NOW, still linked to this issue;
6. **published-review custody agrees** — when an open PR carries the issue's
   published validated work, the PR released must be that one;
7. **the block is the only thing withholding the review** — the review
   validity owner's own answer, as the labels stand and without
   ``blocked-failed``;
8. **checks are green** — the PR's head-commit status rollup reads SUCCESS;
9. **review discovery runs at all** — the review scanner's own answer: with no
   review agent or review label, a released PR would never be queued.

A failed precondition REFUSES the release with a typed
:class:`ReviewReleaseRefusal` and no write: it is recorded as a stale-downgrade
surfaced proposal (``TECH_LEAD_ACTION_PROPOSED``, ``mode="stale_downgrade"``,
``boundary.refusal``) and a skipped result naming the code. The writes
themselves are the shared :class:`~.published_review_release.ReviewReleaseWrites`
(pr-pending on and confirmed first, the PR's review label restored, then
``blocked-failed`` off, guarded) — the same transition the sweep uses, so a
released issue can never become schedulable over its PR. A write that fails
fails the action loudly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from ..domain.pr_attempt_scope import scope_prs_to_active_issue_branch
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from .actions import ActionResult, ReleaseWithheldReviewAction
from .published_review_custody import PublishedReviewHolds
from .published_review_release import ReviewReleaseStatus, ReviewReleaseWrites
from .review_scope import extract_issue_number_from_pr
from .review_validity import evaluate_review_withholding
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced

if TYPE_CHECKING:
    from ..domain.models import SessionHistoryEntry
    from ..infra.config import Config
    from ..ports.issue import Issue
    from .issue_work_claims import IssueWorkClaim
    from ..ports.pull_request_tracker import PRInfo, StatusCheckRollupRead
    from .label_manager import LabelManager
    from .pr_scanner import ReviewAdmission
    from .review_exchange_lifecycle import IssueRuntimeActivity

logger = logging.getLogger(__name__)

OP_TYPE = ReleaseWithheldReviewAction.op_type
_RATIONALE_PREVIEW_CHARS = 500


class ReviewReleaseRefusal(StrEnum):
    """Why a release was refused before any write. Stable codes."""

    REVIEW_DISCOVERY_DISABLED = "review_discovery_disabled"
    LIVE_SESSION = "live_session"
    WORK_CLAIMED = "work_claimed"
    NEWER_FAILURE = "newer_failure"
    ISSUE_UNREADABLE = "issue_unreadable"
    ISSUE_CLOSED = "issue_closed"
    NO_OPEN_PR = "no_open_pr"
    SEVERAL_OPEN_PRS = "several_open_prs"
    PUBLISHED_WORK_ON_ANOTHER_PR = "published_work_on_another_pr"
    REVIEW_NOT_WITHHELD = "review_not_withheld"
    WITHHELD_BY_MORE_THAN_THE_BLOCK = "withheld_by_more_than_the_block"
    CHECKS_UNREADABLE = "checks_unreadable"
    CHECKS_NOT_GREEN = "checks_not_green"
    REVIEW_NOT_DISCOVERABLE = "review_not_discoverable"


@dataclass(frozen=True, slots=True)
class RefusedRelease:
    code: ReviewReleaseRefusal
    detail: str

    def describe(self) -> str:
        return f"{self.code.value}: {self.detail}"

    @property
    def goal_already_met(self) -> bool:
        """Nothing withholds the review any more, so the remedy's goal holds."""
        return self.code is ReviewReleaseRefusal.REVIEW_NOT_WITHHELD


@dataclass(frozen=True, slots=True)
class ReleasableReview:
    issue_number: int
    pr: "PRInfo"

    def describe(self) -> str:
        return (
            f"PR #{self.pr.number} ({self.pr.branch}) is green and withheld from"
            f" review only by issue #{self.issue_number}'s block"
        )


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class TechLeadReviewReleaseExecutor:
    """Applies :class:`ReleaseWithheldReviewAction` after re-verifying it.

    Every collaborator is the owner of one precondition, injected by the
    composition root; this executor owns only the order, the refusal policy
    and the event surface.
    """

    events: EventSink
    config: "Config"
    labels: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    list_open_prs: Callable[[], Sequence["PRInfo"]]
    #: One fresh read of the PR the listing named: the listing carries no
    #: labels or draft flag, and review validity must judge the live PR.
    read_pr: Callable[[int], "PRInfo | None"]
    issue_branches: Callable[[], Mapping[int, str]]
    #: Review discovery's own per-PR gate (scope, active branch), asked of the
    #: fresh PR so a release can never lift a block discovery would ignore.
    review_admission: Callable[["PRInfo", Mapping[int, str]], "ReviewAdmission"]
    read_checks: Callable[[int], "StatusCheckRollupRead"]
    runtime_activity: Callable[[int], "IssueRuntimeActivity"]
    claims_on_issue: Callable[[int], Sequence["IssueWorkClaim"]]
    failures_not_before: Callable[[int, datetime], Sequence["SessionHistoryEntry"]]
    custody: PublishedReviewHolds
    reviews_discoverable: Callable[[], bool]
    writes: ReviewReleaseWrites
    repo_slug: str

    # -- preconditions --------------------------------------------------------

    def verify(
        self, issue_number: int, observed_at: str, source_session_name: str
    ) -> RefusedRelease | ReleasableReview:
        """Every precondition, cheapest first; the first that fails refuses.

        ``source_session_name`` and ``observed_at`` identify the proposing run:
        its own claim on the issue is the only one that does not refuse.
        """
        if not self.reviews_discoverable():
            return RefusedRelease(ReviewReleaseRefusal.REVIEW_DISCOVERY_DISABLED,
                                  "no code-review agent and label are configured, so a"
                                  " released PR would never be queued for review")
        local = self._local_refusal(issue_number, observed_at, source_session_name)
        if local is not None:
            return local
        issue = self.read_issue(issue_number)
        if issue is None:
            return RefusedRelease(ReviewReleaseRefusal.ISSUE_UNREADABLE,
                                  f"issue #{issue_number} could not be read")
        if issue.state != "open":
            return RefusedRelease(ReviewReleaseRefusal.ISSUE_CLOSED,
                                  f"issue #{issue_number} is {issue.state}")
        pr_or_refusal = self._the_open_pr(issue_number)
        if isinstance(pr_or_refusal, RefusedRelease):
            return pr_or_refusal
        return self._review_refusal(issue, pr_or_refusal) or ReleasableReview(issue_number, pr_or_refusal)

    def stale_reason(self, issue_number: int, observed_at: str, source_session_name: str) -> str | None:
        """Read-only applicability, for handing off to an existing proposal."""
        verdict = self.verify(issue_number, observed_at, source_session_name)
        return verdict.describe() if isinstance(verdict, RefusedRelease) else None

    def _local_refusal(
        self, issue_number: int, observed_at: str, source_session_name: str
    ) -> RefusedRelease | None:
        activity = self.runtime_activity(issue_number)
        if activity.busy:
            owners = sorted(kind.value for kind in activity.active | activity.unverifiable)
            return RefusedRelease(ReviewReleaseRefusal.LIVE_SESSION,
                                  f"issue #{issue_number} runtime owners active or unverifiable: {owners}")
        claimed = sorted(
            claim.describe()
            for claim in self.claims_on_issue(issue_number)
            if not claim.held_by(source_session_name, observed_at)
        )
        if claimed:
            return RefusedRelease(ReviewReleaseRefusal.WORK_CLAIMED,
                                  f"issue #{issue_number} has claimed work: {claimed}")
        newer = self.failures_not_before(issue_number, _parse_instant(observed_at))
        if newer:
            seen = ", ".join(
                f"{entry.status} at {entry.completed_at.isoformat() if entry.completed_at else 'an unknown time'}"
                for entry in newer
            )
            return RefusedRelease(ReviewReleaseRefusal.NEWER_FAILURE,
                                  f"issue #{issue_number} failed since the tech lead observed it"
                                  f" at {observed_at}: {seen}")
        return None

    def _the_open_pr(self, issue_number: int) -> "PRInfo | RefusedRelease":
        branches = self.issue_branches()
        linked = [
            pr for pr in self.list_open_prs()
            if extract_issue_number_from_pr(pr, repo_slug=self.repo_slug) == issue_number
        ]
        scoped = scope_prs_to_active_issue_branch(issue_number, linked, issue_branches=branches).matching
        match scoped:
            case (only,):
                return self._fresh(issue_number, only.number, branches)
            case ():
                return RefusedRelease(ReviewReleaseRefusal.NO_OPEN_PR,
                                      f"issue #{issue_number} has no open PR on its active branch")
            case _:
                return RefusedRelease(ReviewReleaseRefusal.SEVERAL_OPEN_PRS,
                                      f"issue #{issue_number} has several open PRs:"
                                      f" {sorted(pr.number for pr in scoped)}")

    def _fresh(
        self, issue_number: int, pr_number: int, branches: Mapping[int, str]
    ) -> "PRInfo | RefusedRelease":
        """The listed PR read fresh, and admitted by review discovery AS IT IS NOW.

        The listing only nominates the PR; its branch or link can change before
        this read, and the issue can leave the configured scope, so discovery's
        gate judges the fresh PR and it must still be this issue's.
        """
        pr = self.read_pr(pr_number)
        if pr is None or pr.state.lower() != "open":
            return RefusedRelease(ReviewReleaseRefusal.NO_OPEN_PR,
                                  f"issue #{issue_number}'s PR #{pr_number} is no longer open")
        admission = self.review_admission(pr, branches)
        if not admission.admitted or admission.issue_number != issue_number:
            return RefusedRelease(ReviewReleaseRefusal.REVIEW_NOT_DISCOVERABLE,
                                  f"review discovery would not review PR #{pr_number} for"
                                  f" issue #{issue_number}: {admission.why}"
                                  f" (linked to #{admission.issue_number})")
        return pr

    def _review_refusal(self, issue: "Issue", pr: "PRInfo") -> RefusedRelease | None:
        holds = self.custody.holds(issue.number)
        if holds and pr.number not in {hold.pr_number for hold in holds}:
            return RefusedRelease(ReviewReleaseRefusal.PUBLISHED_WORK_ON_ANOTHER_PR,
                                  "; ".join(hold.describe() for hold in holds))
        withholding = evaluate_review_withholding(
            config=self.config, label_manager=self.labels, issue=issue, pr=pr,
            block_label=self.labels.blocked_failed,
        )
        if withholding.current.valid:
            return RefusedRelease(ReviewReleaseRefusal.REVIEW_NOT_WITHHELD,
                                  f"review validity already admits PR #{pr.number}")
        if not withholding.withheld_only_by_block:
            return RefusedRelease(
                ReviewReleaseRefusal.WITHHELD_BY_MORE_THAN_THE_BLOCK,
                f"PR #{pr.number} review is {withholding.current.reason}"
                f" (blocking {list(withholding.current.blocking_labels)}) and would still be"
                f" {withholding.without_block.reason} without {self.labels.blocked_failed}",
            )
        checks = self.read_checks(pr.number)
        if checks.capability != "ok":
            return RefusedRelease(ReviewReleaseRefusal.CHECKS_UNREADABLE,
                                  f"PR #{pr.number} checks unreadable: {checks.capability}")
        if checks.state != "SUCCESS":
            return RefusedRelease(ReviewReleaseRefusal.CHECKS_NOT_GREEN,
                                  f"PR #{pr.number} checks are {checks.state or 'absent'}")
        return None

    def _no_longer_releasable(self, action: ReleaseWithheldReviewAction, pr_number: int) -> str | None:
        """The last check before the block comes off: every precondition again.

        The gate and route writes take time. A block that landed, a scope or
        branch change, a new claim or failure, a closed PR: each has an owner,
        and the same owners are asked again here, from fresh reads. The release
        stands only if they still release the SAME PR.
        """
        again = self.verify(action.issue_number, action.observed_at, action.source_session_name)
        if isinstance(again, RefusedRelease):
            return again.describe()
        if again.pr.number != pr_number:
            return f"the issue's releasable PR is now #{again.pr.number}, not #{pr_number}"
        return None

    # -- apply ----------------------------------------------------------------

    def apply(self, action: ReleaseWithheldReviewAction) -> ActionResult:
        verdict = self.verify(action.issue_number, action.observed_at, action.source_session_name)
        if isinstance(verdict, RefusedRelease):
            return self._refuse(action, verdict)
        status, detail = self.writes.release(
            action.issue_number, verdict.pr.number,
            f"tech lead {action.proposal_id} released the review: {verdict.describe()}",
            still_releasable=lambda: self._no_longer_releasable(action, verdict.pr.number),
        )
        if status is not ReviewReleaseStatus.RELEASED:
            logger.error(issue_log(action.issue_number,
                "Tech Lead %s %s FAILED in the release writes: %s"), OP_TYPE, action.proposal_id, detail)
            return ActionResult.fail(
                action, f"review release for issue #{action.issue_number} failed ({status.value}): {detail}",
                issue_number=action.issue_number, proposal_id=action.proposal_id, status=status.value)
        self.events.publish(make_trace_event(EventName.TECH_LEAD_ACTION_EXECUTED, {
            "issue_number": action.anchor_issue_number,
            "action_id": action.proposal_id,
            "proposal_type": OP_TYPE,
            "target_number": action.issue_number,
            "finding_ids": list(action.finding_ids),
            "boundary": {"pr_number": verdict.pr.number, "status": status.value},
        }))
        logger.info(issue_log(action.issue_number, "Tech Lead %s %s released PR #%d's review"),
                    OP_TYPE, action.proposal_id, verdict.pr.number)
        return ActionResult.ok(action, issue_number=action.issue_number,
                               proposal_id=action.proposal_id, pr_number=verdict.pr.number)

    def _refuse(self, action: ReleaseWithheldReviewAction, refused: RefusedRelease) -> ActionResult:
        stale = refused.describe()
        logger.warning(issue_log(action.issue_number, "Tech Lead %s %s refused: %s"),
                       OP_TYPE, action.proposal_id, stale)
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type=OP_TYPE,
            target_number=action.issue_number,
            target_is_pr=False,
            title="",
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=stale,
            boundary={"refusal": refused.code.value},
        )
        return ActionResult.skip(
            action, f"stale precondition: {stale}",
            refusal=refused.code.value,
            terminal_disposition_satisfied=refused.goal_already_met,
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )
