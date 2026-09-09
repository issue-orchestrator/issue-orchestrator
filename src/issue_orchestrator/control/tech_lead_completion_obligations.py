"""Verify immutable investigation obligations independently of lowered work."""
from __future__ import annotations
from typing import Sequence, TYPE_CHECKING
from ..domain.tech_lead_comment import TechLeadCommentIntent
if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import TechLeadDecision
from .actions import Action, ActionResult, ActionResultType
from .required_issue_comment import RequiredTechLeadDiagnosisAction, ReuseTechLeadProposalAction
from .tech_lead_actions import (
    RequireTechLeadInvestigationAction, RecordTechLeadDispositionAction,
    EscalateTechLeadDispositionAction, ResetRetryIssueAction, KillHungSessionAction,
    CreateTechLeadProposalIssueAction, RequestReworkAction,
)


def build_investigation_obligation(decision: TechLeadDecision, *,
        focus_issue_number: int | None) -> RequireTechLeadInvestigationAction:
    """Capture full intended diagnoses before authority lowering can suppress them."""
    if focus_issue_number is None:
        raise ValueError("failure investigation requires its immutable focus")
    return RequireTechLeadInvestigationAction(focus_issue_number=focus_issue_number,
        diagnoses=tuple(TechLeadCommentIntent.from_action(action) for action in decision.proposed_actions
            if action.action_type == "post_comment" and action.target_number == focus_issue_number))


def is_focus_terminal_remedy(action: Action, focus: int) -> bool:
    """One identity rule for the remedies that can satisfy the trusted focus."""
    if isinstance(action, (ResetRetryIssueAction, KillHungSessionAction, EscalateTechLeadDispositionAction, RequestReworkAction)):
        return action.issue_number == focus
    if isinstance(action, RecordTechLeadDispositionAction):
        return action.disposition is not None and action.disposition.issue_number == focus
    if isinstance(action, CreateTechLeadProposalIssueAction):
        return action.op.target_issue_number == focus
    if isinstance(action, ReuseTechLeadProposalAction):
        return action.required_op.target_issue_number == focus
    return False


def scoped_rework_effect_committed(result: ActionResult) -> bool:
    """The scoped executor must attest an effective receipt for this exact target."""
    action = result.action
    if not isinstance(action, RequestReworkAction):
        return False
    request = action.request
    return (result.success
        and result.details.get("rework_status") in {"queued", "active", "completed", "forward_fix"}
        and result.details.get("request_key") == request.key
        and result.details.get("pr_number") == request.target.pr_number
        and result.details.get("head_sha") == request.target.head_sha)


def _effective(result: ActionResult) -> bool:
    if isinstance(result.action, RequestReworkAction):
        return scoped_rework_effect_committed(result)
    return result.success or (result.result_type is ActionResultType.SKIPPED
        and result.details.get("terminal_disposition_satisfied") is True)


def unsatisfied_investigation_obligations(applied: Sequence[ActionResult]) -> tuple[ActionResult, ...]:
    failures: list[ActionResult] = []
    for result in applied:
        contract = result.action
        if not isinstance(contract, RequireTechLeadInvestigationAction):
            continue
        focus = contract.focus_issue_number
        diagnosis = all(any(isinstance(item.action, RequiredTechLeadDiagnosisAction)
            and item.action.intent == intended and item.action.comment == intended.comment
            and item.action.number == focus and not item.action.is_pr and item.success
            for item in applied) for intended in contract.diagnoses)
        remedy = any(is_focus_terminal_remedy(item.action, focus) and _effective(item) for item in applied)
        if not diagnosis or not remedy:
            failures.append(ActionResult.fail(contract,
                f"investigation #{focus} did not satisfy required diagnosis publication and terminal remedy",
                diagnosis_satisfied=diagnosis, remedy_satisfied=remedy,
                pending_disposition=diagnosis and any(is_focus_terminal_remedy(item.action, focus)
                    and item.details.get("pending_disposition") is True for item in applied)))
    return tuple(failures)
