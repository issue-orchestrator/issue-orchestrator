"""One result policy for mandatory tech-lead actions and terminal dispositions.

Reset, kill, dependency wait, gated proposal, and human handoff all use this
boundary. It separates success effects from mandatory work, and distinguishes
an effective investigation remedy from a safe but ineffective stale skip.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Sequence, TYPE_CHECKING
from ..domain.models import SessionStatus
from ..ports.provider_resilience import ProviderErrorType
from ..domain.tech_lead_run_record import TechLeadDeliveryOutcome
from .tech_lead_actions import EscalateTechLeadDispositionAction
if TYPE_CHECKING:
    from .tech_lead_actions import RequireTechLeadInvestigationAction
from .actions import Action, ActionResult, ActionResultType, ResetRetryIssueAction, KillHungSessionAction


class TechLeadHandoffStatus(str, Enum):
    """Whether typed handoff effects proved successful application."""

    ABSENT = "absent"
    APPLIED = "applied"
    UNAPPLIED = "unapplied"


# Explicit terminal proof matrix: every other combination is undelivered.
_DELIVERY_BY_TERMINAL_PROOF = {
    (TechLeadHandoffStatus.ABSENT, SessionStatus.COMPLETED): TechLeadDeliveryOutcome.COMPLETED,
    (TechLeadHandoffStatus.APPLIED, SessionStatus.COMPLETED): TechLeadDeliveryOutcome.HUMAN_HANDOFF,
    (TechLeadHandoffStatus.APPLIED, SessionStatus.BLOCKED): TechLeadDeliveryOutcome.HUMAN_HANDOFF,
    (TechLeadHandoffStatus.APPLIED, SessionStatus.NEEDS_HUMAN): TechLeadDeliveryOutcome.HUMAN_HANDOFF,
}


@dataclass(frozen=True)
class RequiredActLevelOutcome:
    """Did every decision-mandated act-level tech_lead action commit? (ADR-0031 §2).

    The single authoritative boundary the completion path consumes to decide
    terminalization. A planned :class:`ResetRetryIssueAction` is a
    decision-MANDATED act-level mutation: the tech_lead decision required it, so a
    completion is authoritative-success only if it committed. This owner folds
    the applied results into that one verdict so the executor and the
    completion handler cannot drift on what "committed" means.

    A stale general act-level operation remains safe to skip. An investigation
    requires an effective remedy or positive target recovery: a skipped remedy
    alone cannot satisfy it. A durable pending disposition is incomplete but
    owns retry, so it withholds success without adding a separate human gate.
    """

    committed: bool
    human_handoff: TechLeadHandoffStatus = TechLeadHandoffStatus.ABSENT
    failures: tuple[str, ...] = ()
    failed_actions: tuple[Action, ...] = ()
    pending_dispositions: tuple[Action, ...] = ()

    def delivery_outcome(
        self, status: SessionStatus, *,
        provider_error_type: ProviderErrorType | None,
        processing_failed: bool,
    ) -> TechLeadDeliveryOutcome:
        """Preserve terminal provenance before status becomes a display phase.

        A blocked process is never itself a delivery. Only a successfully
        applied typed handoff or a clean completed outcome proves delivery.
        """
        if self.failed or processing_failed or provider_error_type is not None:
            return TechLeadDeliveryOutcome.NOT_DELIVERED
        return _DELIVERY_BY_TERMINAL_PROOF.get(
            (self.human_handoff, status), TechLeadDeliveryOutcome.NOT_DELIVERED
        )

    @property
    def failed(self) -> bool:
        return not self.committed

    def failure_summary(self) -> str:
        return "; ".join(self.failures) or "act-level owner did not commit"


def is_required_act_level_action(action: Action) -> bool:
    """True for a decision-MANDATED act-level action (ADR-0031 §2).

    THE single source of "which actions carry mandated authority", shared by the
    apply-time GATE (:func:`partition_required_act_level_actions`, which withholds
    success-only effects) and the terminal VERDICT
    (:func:`evaluate_required_act_level_outcome`), so authority and the effects it
    gates classify the same actions and cannot drift (#6779 R13). A
    Both wired act-level mutations are mandatory completion gates.
    """
    from .tech_lead_actions import RecordTechLeadDispositionAction, EscalateTechLeadDispositionAction, CreateTechLeadProposalIssueAction
    from .tech_lead_actions import RequireTechLeadInvestigationAction, TechLeadPlanningFailureAction
    from .required_issue_comment import RequiredIssueCommentAction
    return isinstance(action, (RequireTechLeadInvestigationAction, TechLeadPlanningFailureAction, RequiredIssueCommentAction, ResetRetryIssueAction, KillHungSessionAction,
                               RecordTechLeadDispositionAction, EscalateTechLeadDispositionAction,
                               CreateTechLeadProposalIssueAction))


def partition_required_act_level_actions(
    actions: Sequence[Action],
) -> tuple[list[Action], list[Action]]:
    """Split completion actions into (mandated act-level, success-only remainder).

    Relative order within each partition is preserved. The mandated partition is
    the authority gate applied first; the remainder holds the success-only effects
    (labels/comments/close) that must NOT commit unless the gate commits (#6779).
    """
    mandated = [action for action in actions if is_required_act_level_action(action)]
    remainder = [
        action for action in actions if not is_required_act_level_action(action)
    ]
    return mandated, remainder


def require_investigation_terminal_effect(actions: list[Action], *,
        obligation: "RequireTechLeadInvestigationAction") -> list[Action]:
    """Bind only executed exact source diagnoses to the pre-lowering contract."""
    from dataclasses import replace
    from .required_issue_comment import TechLeadDecisionCommentAction, RequiredTechLeadDiagnosisAction
    required: list[Action] = [obligation]
    for action in actions:
        if isinstance(action, (ResetRetryIssueAction, KillHungSessionAction)):
            action = replace(action, requires_effective_disposition=True)
        elif (isinstance(action, TechLeadDecisionCommentAction)
                and action.number == obligation.focus_issue_number and not action.is_pr
                and action.intent in obligation.diagnoses):
            action = RequiredTechLeadDiagnosisAction(number=action.number, comment=action.comment,
                intent=action.intent, reason=action.reason, expected=action.expected)
        required.append(action)
    return required


def evaluate_required_act_level_outcome(
    applied: Sequence[ActionResult],
) -> RequiredActLevelOutcome:
    """Fold applied results into the required-act-level commit verdict.

    Pure over the apply results — the single seam that classifies a mandated
    act-level failure, shared by the completion terminalization path so a
    failed reset can never be recorded as a clean success (#6764 re-review F2).
    """
    from .required_issue_comment import RequiredIssueCommentAction
    from .tech_lead_actions import TechLeadPlanningFailureAction
    failed_results = tuple(
        result
        for result in applied
        if is_required_act_level_action(result.action)
        and (isinstance(result.action, TechLeadPlanningFailureAction)
        or result.result_type is ActionResultType.FAILURE or (
            isinstance(result.action, RequiredIssueCommentAction)
            and result.result_type is not ActionResultType.SUCCESS
        ) or (
            isinstance(result.action, (ResetRetryIssueAction, KillHungSessionAction))
            and result.action.requires_effective_disposition
            and result.result_type is ActionResultType.SKIPPED
            and result.details.get("terminal_disposition_satisfied") is not True
        ))
    )
    from .tech_lead_completion_obligations import unsatisfied_investigation_obligations
    failed_results += unsatisfied_investigation_obligations(applied)
    failures = tuple(
        result.error or "act-level owner failed" for result in failed_results
    )
    return RequiredActLevelOutcome(
        committed=not failures,
        human_handoff=_human_handoff_status(applied),
        failures=failures,
        failed_actions=tuple(result.action for result in failed_results),
        pending_dispositions=tuple(result.action for result in failed_results
            if result.details.get("pending_disposition") is True),
    )


def _human_handoff_status(applied: Sequence[ActionResult]) -> TechLeadHandoffStatus:
    handoffs = [
        result for result in applied
        if isinstance(result.action, EscalateTechLeadDispositionAction)
    ]
    if not handoffs:
        return TechLeadHandoffStatus.ABSENT
    if all(result.result_type is ActionResultType.SUCCESS for result in handoffs):
        return TechLeadHandoffStatus.APPLIED
    return TechLeadHandoffStatus.UNAPPLIED
