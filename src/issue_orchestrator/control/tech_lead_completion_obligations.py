"""Verify immutable investigation obligations independently of lowered work."""
from __future__ import annotations
from typing import Sequence
from .actions import Action, ActionResult, ActionResultType
from .required_issue_comment import RequiredIssueCommentAction, ReuseTechLeadProposalAction
from .tech_lead_actions import (
    RequireTechLeadInvestigationAction, RecordTechLeadDispositionAction,
    EscalateTechLeadDispositionAction, ResetRetryIssueAction, KillHungSessionAction,
    CreateTechLeadProposalIssueAction,
)


def is_focus_terminal_remedy(action: Action, focus: int) -> bool:
    """One identity rule for the remedies that can satisfy the trusted focus."""
    if isinstance(action, (ResetRetryIssueAction, KillHungSessionAction, EscalateTechLeadDispositionAction)):
        return action.issue_number == focus
    if isinstance(action, RecordTechLeadDispositionAction):
        return action.disposition is not None and action.disposition.issue_number == focus
    if isinstance(action, CreateTechLeadProposalIssueAction):
        return action.op.target_issue_number == focus
    if isinstance(action, ReuseTechLeadProposalAction):
        return action.required_op.target_issue_number == focus
    return False


def _effective(result: ActionResult) -> bool:
    return result.success or (result.result_type is ActionResultType.SKIPPED
        and result.details.get("terminal_disposition_satisfied") is True)


def unsatisfied_investigation_obligations(applied: Sequence[ActionResult]) -> tuple[ActionResult, ...]:
    failures: list[ActionResult] = []
    for result in applied:
        contract = result.action
        if not isinstance(contract, RequireTechLeadInvestigationAction):
            continue
        focus = contract.focus_issue_number
        diagnosis = any(isinstance(item.action, RequiredIssueCommentAction)
            and not isinstance(item.action, ReuseTechLeadProposalAction)
            and item.action.number == focus and not item.action.is_pr and item.success
            for item in applied)
        remedy = any(is_focus_terminal_remedy(item.action, focus) and _effective(item) for item in applied)
        if not diagnosis or not remedy:
            failures.append(ActionResult.fail(contract,
                f"investigation #{focus} did not satisfy required diagnosis publication and terminal remedy",
                diagnosis_satisfied=diagnosis, remedy_satisfied=remedy,
                pending_disposition=diagnosis and any(is_focus_terminal_remedy(item.action, focus)
                    and item.details.get("pending_disposition") is True for item in applied)))
    return tuple(failures)
