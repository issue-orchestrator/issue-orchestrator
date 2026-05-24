"""State-table tests for completion finalization policy."""

from __future__ import annotations

import pytest

from issue_orchestrator.domain.completion_finalization import (
    CompletionFinalizationCommand,
    CompletionFinalizationDecision,
    CompletionRuntimeState,
    ReviewExchangeRunningQuery,
    decide_completion_finalization,
)
from issue_orchestrator.domain.models import CompletionOutcome, RequestedAction


def _command(
    *,
    outcome: CompletionOutcome = CompletionOutcome.COMPLETED,
    requested_actions: tuple[RequestedAction, ...] = (
        RequestedAction.PUSH_BRANCH,
    ),
    runtime_state: CompletionRuntimeState = CompletionRuntimeState.TERMINATED,
    review_exchange_running: bool = False,
    validation_preflight_configured: bool = False,
    review_exchange_within_deadline: bool = False,
) -> CompletionFinalizationCommand:
    return CompletionFinalizationCommand(
        issue_number=364,
        session_name="issue-364",
        outcome=outcome,
        requested_actions=requested_actions,
        runtime_state=runtime_state,
        review_exchange_running=review_exchange_running,
        validation_preflight_configured=validation_preflight_configured,
        review_exchange_within_deadline=review_exchange_within_deadline,
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            _command(
                requested_actions=(
                    RequestedAction.PUSH_BRANCH,
                    RequestedAction.CREATE_PR,
                ),
                review_exchange_running=True,
                validation_preflight_configured=True,
            ),
            CompletionFinalizationDecision.DEFER_REVIEW_EXCHANGE,
        ),
        (
            # Outer session TIMED_OUT, BG job has overshot its own deadline
            # too — terminal cancel (pre-existing behavior preserved).
            _command(
                requested_actions=(RequestedAction.CREATE_PR,),
                runtime_state=CompletionRuntimeState.TIMED_OUT,
                review_exchange_running=True,
                review_exchange_within_deadline=False,
            ),
            CompletionFinalizationDecision.TERMINAL_REVIEW_EXCHANGE_TIMEOUT,
        ),
        (
            # Outer session TIMED_OUT, but BG exchange is still within its
            # own much larger supervisor budget — defer to the BG job
            # instead of cancelling healthy in-flight work. Repro: tixmeup
            # #276/#284/#289 — the outer 90-min coder timeout fired while
            # a multi-round review exchange was still making progress.
            _command(
                requested_actions=(RequestedAction.CREATE_PR,),
                runtime_state=CompletionRuntimeState.TIMED_OUT,
                review_exchange_running=True,
                review_exchange_within_deadline=True,
            ),
            CompletionFinalizationDecision.DEFER_REVIEW_EXCHANGE,
        ),
        (
            _command(
                requested_actions=(RequestedAction.PUSH_BRANCH,),
                validation_preflight_configured=True,
            ),
            CompletionFinalizationDecision.RUN_DIRTY_PREFLIGHT,
        ),
        (
            _command(
                requested_actions=(RequestedAction.PUSH_BRANCH,),
                runtime_state=CompletionRuntimeState.TIMED_OUT,
                review_exchange_running=False,
                validation_preflight_configured=True,
            ),
            CompletionFinalizationDecision.RUN_DIRTY_PREFLIGHT,
        ),
        (
            _command(
                requested_actions=(RequestedAction.PUSH_BRANCH,),
                validation_preflight_configured=False,
            ),
            CompletionFinalizationDecision.PROCESS,
        ),
        (
            _command(
                requested_actions=(RequestedAction.CREATE_PR,),
                runtime_state=CompletionRuntimeState.TIMED_OUT,
                review_exchange_running=False,
                validation_preflight_configured=False,
            ),
            CompletionFinalizationDecision.PROCESS,
        ),
        (
            _command(
                outcome=CompletionOutcome.BLOCKED,
                requested_actions=(RequestedAction.CREATE_PR,),
                review_exchange_running=True,
                validation_preflight_configured=True,
            ),
            CompletionFinalizationDecision.PROCESS,
        ),
    ],
)
def test_completion_finalization_matrix(
    command: CompletionFinalizationCommand,
    expected: CompletionFinalizationDecision,
) -> None:
    assert decide_completion_finalization(command).decision is expected


def test_review_exchange_running_query_requires_tuple_actions() -> None:
    with pytest.raises(TypeError, match="requested_actions must be a tuple"):
        ReviewExchangeRunningQuery(
            issue_number=364,
            session_name="issue-364",
            requested_actions=[RequestedAction.CREATE_PR],  # type: ignore[arg-type]
        )


def test_completion_command_rejects_wrong_action_type() -> None:
    with pytest.raises(TypeError, match="RequestedAction"):
        CompletionFinalizationCommand(
            issue_number=364,
            session_name="issue-364",
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=("create_pr",),  # type: ignore[arg-type]
            runtime_state=CompletionRuntimeState.TERMINATED,
            review_exchange_running=False,
            validation_preflight_configured=False,
        )


def test_completion_command_requires_positive_issue_number() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        CompletionFinalizationCommand(
            issue_number=0,
            session_name="issue-0",
            outcome=CompletionOutcome.COMPLETED,
            requested_actions=(),
            runtime_state=CompletionRuntimeState.TERMINATED,
            review_exchange_running=False,
            validation_preflight_configured=False,
        )
