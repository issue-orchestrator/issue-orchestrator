"""One owner for approved scoped rework, including partial-write reconciliation.

The durable instruction is written before remote effects; needs-rework is the
LAST write. Normal discovery then supplies that instruction to the existing
rework launcher, retaining all routing, dependency and cycle policy. The owner
never changes a branch/worktree or directly enqueues/launches a session.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Sequence
from ..ports.issue import Issue
from ..ports.pull_request_tracker import PRInfo
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .scoped_rework_proposals import ReworkProposalView
from ..domain.scoped_rework import (
    TechLeadProposalCommand,
    TechLeadProposalCommandOutcome,
)

from ..domain.scoped_rework import ReworkReceipt, ReworkRequest
from ..domain.session_run import SessionRunIdentity
from ..events import EventName
from ..ports import EventSink, RepositoryHost, make_trace_event
from ..ports.tech_lead_authority import TechLeadAuthorityStore
from .actions import Action, ActionResult, AddCommentAction, AddLabelAction, RemoveLabelAction, RequestReworkAction
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired
from .in_flight_work import SettlementOutcome
from .label_manager import LabelManager
from .pending_work_successors import PendingWorkSuccessors
from .needs_human_block import BlockOutcome, SharedNeedsHumanBlock
from .scoped_rework_eligibility import rework_target_stale_reason
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced


@dataclass
class RequestReworkExecutor:
    repository: RepositoryHost
    receipts: TechLeadAuthorityStore
    labels: LabelManager
    block: SharedNeedsHumanBlock
    events: EventSink
    is_active: Callable[[int], bool]
    mutate: Callable[[RequestReworkAction, Action], ActionResult]
    before_write: Callable[[RequestReworkAction], None]
    pending_successors: PendingWorkSuccessors
    filtering_label: str = ""
    is_attempt_active: Callable[[int, SessionRunIdentity], bool] | None = None

    def proposal_views(self) -> tuple["ReworkProposalView", ...]:
        from .scoped_rework_proposals import project_rework_proposals

        return project_rework_proposals(self)

    def proposal_command(
        self, command: TechLeadProposalCommand
    ) -> TechLeadProposalCommandOutcome:
        from .tech_lead_proposals import apply_tech_lead_proposal_command

        return apply_tech_lead_proposal_command(
            command, repository=self.repository, ops=self.receipts
        )

    def validate_proposal_reuse(self, number: int, required: ReworkRequest) -> None:
        """Prove the exact immutable proposal or its current durable successor."""
        receipt = self.receipts.load_rework_receipt(required.key)
        if receipt is not None:
            if receipt.proposal_issue_number != number:
                raise ValueError("rework receipt belongs to a different proposal")
            self._validate_receipt_owner(receipt)
            return
        stored = self.receipts.load_op(issue_number=number)
        if stored is None or stored.rework_request is None or stored.rework_request.key != required.key:
            raise ValueError("existing proposal does not own the required scoped finding/head")
        proposal = self.repository.get_issue(number)
        if proposal is None or proposal.state != "open":
            raise ValueError("existing scoped proposal is missing or closed")
        target = stored.rework_request.target
        stale = self.stale_reason(stored.rework_request,
            self.repository.get_pr(target.pr_number), self.repository.get_issue(target.issue_number))
        if stale:
            raise ValueError(f"existing scoped proposal is no longer applicable: {stale}")

    def _validate_receipt_owner(self, receipt: ReworkReceipt) -> None:
        from .scoped_rework_reuse import validate_receipt_owner
        validate_receipt_owner(self, receipt)

    def apply(self, action: RequestReworkAction) -> ActionResult:
        request = action.request
        previous = self.receipts.load_rework_receipt(request.key)
        if previous is not None:
            # The first approved instruction wins, even if a repeated decision
            # paraphrases it. The identity cannot authorize replacement work.
            request = previous.request
            # Reconciliation commands may omit proposal provenance. The durable
            # original owns it across intermediate, rejected and terminal writes.
            action = replace(action, request=request,
                proposal_issue_number=previous.proposal_issue_number)
            if previous.was_rejected:
                return self._stale(action, previous.detail)
            if previous.was_unsuccessful:
                return ActionResult.fail(
                    action, previous.detail, request_key=request.key
                )
            if not previous.requires_reconciliation:
                return self._success(action, previous)
        target = request.target
        try:
            pr = self.repository.get_pr(target.pr_number)
            issue = self.repository.get_issue(target.issue_number)
            stale = self.stale_reason(request, pr, issue)
            if stale:
                return self._stale(action, stale)
            assert pr is not None and issue is not None
            self.receipts.save_rework_receipt(
                ReworkReceipt(
                    request,
                    "executing",
                    proposal_issue_number=action.proposal_issue_number,
                )
            )
            if pr.state == "merged":
                receipt = self._forward_fix(action, issue)
            else:
                self._prepare_open(action, issue.labels)
                status = "queued"
                receipt = ReworkReceipt(request, status)
            receipt = replace(
                receipt, proposal_issue_number=action.proposal_issue_number
            )
            self.receipts.save_rework_receipt(receipt)
            return self._success(action, receipt)
        except (ClaimLostError, ReconciliationRequired):
            raise
        except Exception as exc:
            return ActionResult.fail(
                action, f"Scoped rework did not finish: {exc}", request_key=request.key
            )

    def stale_reason(self, request: ReworkRequest, pr: PRInfo | None, issue: Issue | None) -> str | None:
        return rework_target_stale_reason(request, pr, issue)

    def _prepare_open(
        self, action: RequestReworkAction, issue_labels: Sequence[str]
    ) -> None:
        request = action.request
        target = request.target
        marker = f"<!-- scoped-rework:{request.key} -->"
        if not self.repository.issue_comment_marker_present(target.pr_number, marker):
            self._mutate(action, AddCommentAction(
                number=target.pr_number, is_pr=True,
                comment=f"{marker}\n## Approved scoped rework\n\n{request.feedback}\n\n{request.report}",
                expected=action.expected,
            ))
        # Revalidate just before the first workflow mutation; no cache is used
        # by RepositoryHost.get_pr. A changed head never inherits this request.
        current = self.repository.get_pr(target.pr_number)
        if (
            current is None
            or current.state != "open"
            or current.head_sha != target.head_sha
        ):
            raise RuntimeError(
                "PR changed during feedback publication; retry for stale classification"
            )
        invalidated = {self.labels.code_reviewed, self.labels.tech_lead_reviewed}
        for label in invalidated.intersection(current.labels):
            self._mutate(action, RemoveLabelAction(issue_number=target.pr_number, label=label, expected=action.expected))
        for number, observed, live in (
            (target.pr_number, target.pr_labels, current.labels),
            (target.issue_number, target.issue_labels, issue_labels),
        ):
            if self.labels.needs_human in observed and self.labels.needs_human in live:
                outcome = self.block.clear_observed_operator_block(
                    number, "approved scoped rework", before_write=lambda: self.before_write(action)
                )
                if outcome in {BlockOutcome.FAILED, BlockOutcome.UNGOVERNED}:
                    raise RuntimeError(
                        "shared human-block owner could not reconcile the approved block"
                    )
        for label in {self.labels.needs_rework}.difference(current.labels):
            self._mutate(action, AddLabelAction(issue_number=target.pr_number, label=label, expected=action.expected))

    def _mutate(self, action: RequestReworkAction, mutation: Action) -> None:
        result = self.mutate(action, mutation)
        if not result.success:
            raise RuntimeError(f"Scoped mutation did not commit: {result.error}")

    def _forward_fix(self, action: RequestReworkAction, issue: Issue) -> ReworkReceipt:
        request = action.request
        target = request.target
        marker = f"<!-- scoped-rework-forward-fix:{request.key} -->"
        title = f"{issue.title} — forward fix for PR #{target.pr_number}"
        number = self.repository.find_issue_by_marker(
            title=title, marker=marker, authoritative=True
        )
        if number is None:
            # Preserve agent, priority, filtering and milestone routing, while
            # shedding lifecycle/block labels that describe the original work.
            labels = [
                label
                for label in issue.labels
                if label.startswith(("agent:", "priority:"))
            ]
            labels = sorted(set(labels).union(filter(None, (self.filtering_label,))))
            self.before_write(action)
            result = self.repository.create_issue(
                title=title,
                body=f"{marker}\nFix the finding from merged PR #{target.pr_number} (issue #{target.issue_number}).\n\n{request.feedback}\n\n{request.report}",
                labels=labels,
                milestone=issue.milestone_number,
            )
            if not result or not isinstance(result.get("number"), int):
                raise RuntimeError(
                    "forward-fix issue creation returned no issue identity"
                )
            number = result["number"]
        return ReworkReceipt(request, "forward_fix", number)

    def _success(
        self, action: RequestReworkAction, receipt: ReworkReceipt
    ) -> ActionResult:
        details = {
            "request_key": receipt.request.key,
            "rework_status": receipt.status,
            "pr_number": receipt.request.target.pr_number,
            "head_sha": receipt.request.target.head_sha,
            "forward_issue_number": receipt.forward_issue_number,
        }
        self.events.publish(
            make_trace_event(
                EventName.TECH_LEAD_ACTION_EXECUTED,
                {
                    "issue_number": action.anchor_issue_number,
                    "action_id": action.proposal_id,
                    "proposal_type": "request_rework",
                    "target_number": action.issue_number,
                    "finding_ids": list(action.finding_ids),
                    "boundary": details,
                },
            )
        )
        return ActionResult.ok(
            action,
            request_key=receipt.request.key,
            rework_status=receipt.status,
            pr_number=receipt.request.target.pr_number,
            head_sha=receipt.request.target.head_sha,
            forward_issue_number=receipt.forward_issue_number,
        )

    def _stale(self, action: RequestReworkAction, reason: str) -> ActionResult:
        self.receipts.save_rework_receipt(
            ReworkReceipt(
                action.request,
                "stale",
                proposal_issue_number=action.proposal_issue_number,
                detail=reason,
            )
        )
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type="request_rework",
            target_number=action.request.target.pr_number,
            target_is_pr=True,
            title="",
            body_preview=action.request.feedback[:500],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=reason,
        )
        return ActionResult.skip(action, reason, mode=STALE_DOWNGRADE_MODE)


def note_scoped_rework_started(
    receipts: TechLeadAuthorityStore, identity: SessionRunIdentity
) -> None:
    """Adopt only the exact durable selection claimed before this run spawned."""
    receipts.save_rework_receipts(tuple(
        replace(receipt, status="active", detail="Rework run is active")
        for receipt in receipts.list_rework_receipts()
        if receipt.consumed_by(identity)
    ))


def note_scoped_rework_finished(
    receipts: TechLeadAuthorityStore, identity: SessionRunIdentity, completed: bool,
    *, work_outcome: SettlementOutcome,
) -> None:
    """Only the matching effective terminal outcome can mark a request complete."""
    if work_outcome is SettlementOutcome.PROVIDER_DEFERRED:
        # The exact durable deferred claim owns transfer; keep this attempt
        # bound across restart until that claim is reclaimed.
        return
    for receipt in receipts.list_rework_receipts():
        if receipt.consumed_by(identity):
            receipts.save_rework_receipt(
                replace(
                    receipt,
                    status="completed" if completed else "failed",
                    detail="Rework completed"
                    if completed
                    else "Rework attempt failed; normal failure policy applies",
                )
            )
