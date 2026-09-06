"""Mandatory publication commands and applicable proposal reuse ownership."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from .actions import AddCommentAction, ActionResult

if TYPE_CHECKING:
    from ..domain.tech_lead_session import StoredTechLeadOp
    from ..ports import RepositoryHost, EventSink
    from .actions import Action
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .tech_lead_reset_retry import TechLeadResetRetryExecutor
    from .tech_lead_kill_session import TechLeadKillSessionExecutor


@dataclass(frozen=True)
class RequiredIssueCommentAction(AddCommentAction):
    """Completion requires an exact, authenticated publication receipt."""


@dataclass(frozen=True)
class ReuseTechLeadProposalAction(RequiredIssueCommentAction):
    """An existing proposal must still own this exact applicable remedy."""
    required_op: StoredTechLeadOp = field(kw_only=True)


def validate_proposal_reuse(action: ReuseTechLeadProposalAction, *,
        host: RepositoryHost, authority: TechLeadAuthorityStore | None,
        reset: TechLeadResetRetryExecutor | None,
        kill: TechLeadKillSessionExecutor | None) -> None:
    if authority is None:
        raise ValueError("proposal reuse requires the authority owner")
    stored = authority.load_op(issue_number=action.number)
    required = action.required_op
    if stored is None or (stored.op_type, stored.target_issue_number,
            stored.target_session_id, stored.target_terminal_id, stored.target_session_type) != (
            required.op_type, required.target_issue_number, required.target_session_id,
            required.target_terminal_id, required.target_session_type):
        raise ValueError("existing proposal does not own the required remedy generation")
    if host.get_issue_state(action.number) != "open":
        raise ValueError("existing proposal is missing or closed")
    if host.get_issue_state(stored.target_issue_number) != "open":
        raise ValueError("proposal target is missing or closed")
    if stored.op_type == "reset_retry" and reset is not None:
        stale = reset.stale_reason(stored.target_issue_number)
    elif stored.op_type == "kill_hung_session" and kill is not None:
        from ..domain.tech_lead_session import TechLeadSessionGeneration
        from ..domain.session_key import TaskKind
        target = TechLeadSessionGeneration(issue_number=stored.target_issue_number,
            task_kind=TaskKind(stored.target_session_type), terminal_id=stored.target_terminal_id,
            run_id=stored.target_session_id)
        stale = kill.proposal_stale_reason(target)
    else:
        raise ValueError("proposal reuse has no live applicability owner")
    if stale is not None:
        raise ValueError(f"existing proposal is no longer applicable: {stale}")


def apply_required_issue_comment(action: RequiredIssueCommentAction, *,
        host: RepositoryHost, guard: Callable[[], None],
        post_comment: Callable[[int, str], str]) -> ActionResult:
    """Verify receipts around the guarded, applier-owned publication capability."""
    try:
        guard()
        receipt = host.find_issue_comment_receipt(action.number, body=action.comment)
        guard()
        if receipt is None:
            post_comment(action.number, action.comment)
            receipt = host.find_issue_comment_receipt(action.number, body=action.comment)
        guard()
        if receipt is None:
            raise ValueError("required explanation has no verified publication receipt")
        return ActionResult.ok(action, comment_id=receipt.comment_id,
            body_sha256=receipt.body_sha256, author_key=receipt.author_key)
    except Exception as exc:
        return ActionResult.fail(action, str(exc))


def apply_issue_comment(action: AddCommentAction, *, host: RepositoryHost,
        post_comment: Callable[[int, str], str],
        require_expected: Callable[[Action, int], None],
        verify_claim: Callable[[Action, int], None], events: EventSink,
        authority: TechLeadAuthorityStore | None,
        reset: TechLeadResetRetryExecutor | None,
        kill: TechLeadKillSessionExecutor | None) -> ActionResult:
    """One publication owner for ordinary and mandatory issue explanations."""
    def guard() -> None:
        require_expected(action, action.number)
        verify_claim(action, action.number)
        if isinstance(action, ReuseTechLeadProposalAction):
            require_expected(action, action.required_op.target_issue_number)
            verify_claim(action, action.required_op.target_issue_number)
            validate_proposal_reuse(action, host=host, authority=authority, reset=reset, kill=kill)
    if isinstance(action, RequiredIssueCommentAction):
        return apply_required_issue_comment(action, host=host, guard=guard, post_comment=post_comment)
    # Ordinary comments preserve their reconciliation exception contract.
    guard()
    try:
        url = post_comment(action.number, action.comment)
        if action.is_pr:
            from ..ports import make_trace_event
            from ..events import EventName
            events.publish(make_trace_event(EventName.REVIEW_COMMENT_ADDED, {
                "issue_number": action.number, "pr_number": action.number,
                "comment_url": url, "comment_excerpt": action.comment.strip().replace("\n", " "),
                "summary": "Posted review comment",
            }))
        return ActionResult.ok(action, number=action.number, is_pr=action.is_pr)
    except Exception as exc:
        return ActionResult.fail(action, str(exc))
