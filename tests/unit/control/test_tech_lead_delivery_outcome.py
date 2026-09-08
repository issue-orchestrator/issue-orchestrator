"""Delivery proof comes from typed terminal effects, never display status."""

import pytest

from issue_orchestrator.control.actions import ActionResult, ActionResultType
from issue_orchestrator.control.tech_lead_actions import (
    EscalateTechLeadDispositionAction,
)
from issue_orchestrator.control.tech_lead_reset_retry import (
    required_act_level_outcome_after_apply,
)
from issue_orchestrator.domain.models import SessionStatus
from issue_orchestrator.domain.tech_lead_run_record import TechLeadDeliveryOutcome
from issue_orchestrator.ports.provider_resilience import ProviderErrorType


@pytest.mark.parametrize(
    "status",
    [SessionStatus.COMPLETED, SessionStatus.BLOCKED, SessionStatus.NEEDS_HUMAN],
)
@pytest.mark.parametrize("result_type", list(ActionResultType))
def test_handoff_requires_a_successful_typed_effect(status, result_type):
    action = EscalateTechLeadDispositionAction(
        issue_number=1, comment="A human decision is required"
    )
    outcome = required_act_level_outcome_after_apply(
        [ActionResult(action, result_type)], None
    )
    assert outcome.delivery_outcome(
        status, provider_error_type=None, processing_failed=False
    ) is (
        TechLeadDeliveryOutcome.HUMAN_HANDOFF
        if result_type is ActionResultType.SUCCESS
        else TechLeadDeliveryOutcome.NOT_DELIVERED
    )


@pytest.mark.parametrize(
    "status", [SessionStatus.BLOCKED, SessionStatus.NEEDS_HUMAN, SessionStatus.FAILED]
)
def test_status_without_an_applied_handoff_cannot_supply_delivery(status):
    outcome = required_act_level_outcome_after_apply([], None)
    assert (
        outcome.delivery_outcome(
            status, provider_error_type=None, processing_failed=False
        )
        is TechLeadDeliveryOutcome.NOT_DELIVERED
    )


@pytest.mark.parametrize("failure", ["provider", "processing", "apply"])
def test_terminal_failures_withhold_even_a_successful_handoff(failure):
    action = EscalateTechLeadDispositionAction(
        issue_number=1, comment="A human decision is required"
    )
    outcome = required_act_level_outcome_after_apply(
        [ActionResult.ok(action)],
        RuntimeError("apply aborted") if failure == "apply" else None,
    )
    assert (
        outcome.delivery_outcome(
            SessionStatus.COMPLETED,
            provider_error_type=ProviderErrorType.AUTH
            if failure == "provider"
            else None,
            processing_failed=failure == "processing",
        )
        is TechLeadDeliveryOutcome.NOT_DELIVERED
    )
