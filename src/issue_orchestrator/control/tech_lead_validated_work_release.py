"""Execute an approved ``release_validated_work`` through the abandonment owner (#9092).

Work republished on another PR of its issue is released by proof (#8137).
A PR later rebased with conflicts resolved carries none of the parked heads,
so nothing proves the work survived; only the operator can say it did. This
owner turns that approved statement into exactly one effect: the named
records resolve ABANDONED, all or none, naming the PR that rebuilt them and
the maintainer who approved it, and the issue's aggregate block reprojects.

Agent intent, orchestrator authority: the tech lead names records and a PR;
the orchestrator bound each record to its launch-observed snapshot when the
proposal was filed, and here re-verifies, before any write, that

1. a maintainer's approval of this proposal is verified (else a wiring bug);
2. the superseding PR is a MERGED PR of the issue in this repository, on one
   of the issue's own branches (else stale: the proposal closes, no change).

The store's CAS (``abandon_all_if_current``) then compares every record's
snapshot inside the write transaction: a record that moved since launch
refuses the whole release. A release these very commands already committed
is a replay: it is recognized FIRST, before any remote read, so a retry after
the commit (a failed reprojection or proposal finalization) completes the
operation whatever GitHub answers now, and never reads its own write as
stale. Proposal reuse, which writes nothing, asks :meth:`stale_reason`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..domain.publication_remote import (
    PublicationPrState,
    PublicationPullRequest,
    PublicationRemoteError,
)
from ..domain.tech_lead_approval import ApprovalVerdict
from ..domain.validated_work_capture import ValidatedWorkRemoteRequest
from ..domain.validated_work_commands import (
    AbandonAllOutcome,
    AbandonStatus,
    AbandonValidatedWorkCommand,
)
from ..domain.validated_work_release import (
    RELEASE_VALIDATED_WORK_ACTION,
    ValidatedWorkRelease,
)
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.validated_work_recovery_authority import (
    ValidatedWorkRecoveryAuthorityReader,
)
from .actions import ActionResult, ReleaseValidatedWorkAction
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced

logger = logging.getLogger(__name__)
_RATIONALE_PREVIEW_CHARS = 500


@dataclass
class TechLeadValidatedWorkReleaseExecutor:
    """Re-verify an approved release, then abandon its records atomically."""

    events: EventSink
    #: The current releasable snapshots, from the same owner as launch grants.
    grants: ValidatedWorkRecoveryAuthorityReader
    #: Uncached reads of the issue's PRs (the #8137 issue-PR walk).
    pull_requests: ValidatedWorkCaptureObserver
    #: The verified approval of a proposal issue; raises when there is none.
    approval: Callable[[int], ApprovalVerdict]
    abandon_all: Callable[[tuple[AbandonValidatedWorkCommand, ...]], AbandonAllOutcome]
    #: Whether exactly these commands already committed (the owner's replay test).
    committed: Callable[[tuple[AbandonValidatedWorkCommand, ...]], bool]

    def stale_reason(self, release: ValidatedWorkRelease) -> str | None:
        """Why the bound snapshots no longer describe the records, or None.

        Read-only, for proposal reuse. :meth:`apply` leaves this to the
        store's CAS, which also recognizes its own committed release.
        """
        current = set(self.grants.release_grants_for((release.issue_number,)))
        moved = [item.record_id for item in release.authorities if item not in current]
        if not moved:
            return None
        return (
            f"{len(moved)} of {len(release.authorities)} named record(s) are no"
            f" longer releasable as approved (resolved, publishing, or their"
            f" evidence moved): {', '.join(moved)}"
        )

    def apply(self, action: ReleaseValidatedWorkAction) -> ActionResult:
        release = action.release
        verdict = self.approval(action.proposal_issue_number)
        actor = (
            f"{verdict.describe()} on tech-lead proposal"
            f" #{action.proposal_issue_number}"
        )
        reason = release.resolution_reason()
        commands = tuple(
            AbandonValidatedWorkCommand(authority, actor, reason)
            for authority in release.authorities
        )
        if not self.committed(commands):
            refused = self._verify_superseding_pr(action)
            if refused is not None:
                return refused
        outcome = self.abandon_all(commands)
        if outcome.refusal is not None:
            return self._refused(action, outcome)
        # Who released the records is what the store recorded: on a replay, the
        # approver of the commit, not whoever re-approved the retry.
        recorded = outcome.abandoned[0].disposition
        assert recorded is not None and recorded.resolution is not None  # ABANDONED carries both
        self._publish_executed(action, recorded.resolution.actor, replayed=outcome.replayed)
        logger.info(
            issue_log(
                action.issue_number,
                "Tech Lead release_validated_work %s released %d record(s)"
                " rebuilt in PR #%d%s",
            ),
            action.proposal_id,
            len(release.authorities),
            release.superseding_pr_number,
            " (replayed)" if outcome.replayed else "",
        )
        return ActionResult.ok(
            action,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
            terminal_disposition_satisfied=True,
            released_record_ids=list(release.record_ids),
            pr_number=release.superseding_pr_number,
            replayed=outcome.replayed,
        )

    def _verify_superseding_pr(self, action: ReleaseValidatedWorkAction) -> ActionResult | None:
        """None when the named PR can have rebuilt the work, else the result."""
        release = action.release
        try:
            refusal = self._superseding_refusal(release)
        except PublicationRemoteError as exc:
            # An unreadable remote proves nothing either way: keep the
            # approved op and retry, never release on an unanswered read.
            return ActionResult.fail(
                action,
                f"superseding PR #{release.superseding_pr_number} could not be"
                f" read: {exc}",
                issue_number=action.issue_number,
                proposal_id=action.proposal_id,
            )
        return self._downgrade(action, refusal) if refusal is not None else None

    def _superseding_refusal(self, release: ValidatedWorkRelease) -> str | None:
        """Why the named PR cannot have rebuilt this issue's work, or None."""
        number = release.superseding_pr_number
        pull = self._find_issue_pull_request(release)
        if pull is None:
            return (
                f"PR #{number} is not an open or merged PR of issue"
                f" #{release.issue_number} on one of its own branches"
            )
        if pull.head_repo != release.repo_slug or pull.base_repo != release.repo_slug:
            return f"PR #{number} is not a PR of {release.repo_slug}"
        if pull.state is not PublicationPrState.MERGED:
            return (
                f"PR #{number} is {pull.state.value}, not merged: only merged"
                " work can stand in for the records it rebuilt"
            )
        return None

    def _find_issue_pull_request(
        self, release: ValidatedWorkRelease
    ) -> PublicationPullRequest | None:
        """The named PR among the issue's PRs, read uncached, or None.

        The issue-PR walk covers every branch of the issue except the one
        asked about, so a PR on a record's own branch is found among that
        branch's merged PRs instead.
        """
        number = release.superseding_pr_number
        for branch in release.branch_names:
            request = ValidatedWorkRemoteRequest(
                release.repo_slug, release.issue_number, branch
            )
            for pull in self.pull_requests.issue_pull_requests(request):
                if pull.number == number:
                    return pull
            for pull in self.pull_requests.merged_pull_requests(request):
                if pull.number == number:
                    return pull
        return None

    def _refused(
        self, action: ReleaseValidatedWorkAction, outcome: AbandonAllOutcome
    ) -> ActionResult:
        refusal = outcome.refusal
        assert refusal is not None
        message = f"record {outcome.refused_record_id}: {refusal.message}"
        if refusal.status is AbandonStatus.BUSY:
            # Another owner holds the record or the issue right now; nothing
            # moved, so the approval stands and the op is retried.
            return ActionResult.fail(
                action,
                f"release deferred, {message}",
                issue_number=action.issue_number,
                proposal_id=action.proposal_id,
            )
        return self._downgrade(action, f"{refusal.status.value}: {message}")

    def _publish_executed(
        self, action: ReleaseValidatedWorkAction, actor: str, *, replayed: bool
    ) -> None:
        release = action.release
        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_ACTION_EXECUTED,
                {
                    "issue_number": action.anchor_issue_number,
                    "action_id": action.proposal_id,
                    "proposal_type": RELEASE_VALIDATED_WORK_ACTION,
                    "target_number": action.issue_number,
                    "finding_ids": list(action.finding_ids),
                    "boundary": {
                        "record_ids": list(release.record_ids),
                        "superseding_pr_number": release.superseding_pr_number,
                        "actor": actor,
                        "replayed": replayed,
                    },
                },
            )
        )

    def _downgrade(self, action: ReleaseValidatedWorkAction, reason: str) -> ActionResult:
        logger.warning(
            issue_log(
                action.issue_number,
                "Tech Lead release_validated_work %s downgraded: %s",
            ),
            action.proposal_id,
            reason,
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type=RELEASE_VALIDATED_WORK_ACTION,
            target_number=action.issue_number,
            target_is_pr=False,
            title="",
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=reason,
            boundary=action.release.to_dict(),
        )
        return ActionResult.skip(
            action,
            f"stale precondition: {reason}",
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )
