"""Completion finalization decision policy.

Owns the single place that answers: after an agent writes a completion record,
what finalization step is safe to run next?

The matrix is deliberately pure. Callers gather runtime facts such as whether a
review-exchange job is already running, construct ``CompletionFinalizationCommand``,
and dispatch on the returned ``CompletionFinalizationDecision``. This prevents
controller and processor paths from drifting on the ordering between review
exchange deferral, dirty-worktree preflight, and publish preconditions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import CompletionOutcome, RequestedAction


class CompletionRuntimeState(Enum):
    """Runtime observation state relevant to completion finalization."""

    TERMINATED = "terminated"
    TIMED_OUT = "timed_out"


class CompletionFinalizationDecision(Enum):
    """The next finalization step the caller should take."""

    PROCESS = "process"
    DEFER_REVIEW_EXCHANGE = "defer_review_exchange"
    TERMINAL_REVIEW_EXCHANGE_TIMEOUT = "terminal_review_exchange_timeout"
    RUN_DIRTY_PREFLIGHT = "run_dirty_preflight"


@dataclass(frozen=True)
class ReviewExchangeRunningQuery:
    """Typed query for review-exchange jobs that may already be in flight."""

    issue_number: int
    session_name: str | None
    requested_actions: tuple[RequestedAction, ...]

    def __post_init__(self) -> None:
        _require_positive_issue_number(self.issue_number)
        _require_session_name(self.session_name)
        _require_requested_actions(self.requested_actions)

    @property
    def requires_review_exchange(self) -> bool:
        return RequestedAction.CREATE_PR in self.requested_actions


@dataclass(frozen=True)
class CompletionFinalizationCommand:
    """Pure command describing one completion finalization decision."""

    issue_number: int
    session_name: str | None
    outcome: CompletionOutcome
    requested_actions: tuple[RequestedAction, ...]
    runtime_state: CompletionRuntimeState
    review_exchange_running: bool
    validation_preflight_configured: bool

    def __post_init__(self) -> None:
        _require_positive_issue_number(self.issue_number)
        _require_session_name(self.session_name)
        _require_completion_outcome(self.outcome)
        _require_requested_actions(self.requested_actions)
        _require_runtime_state(self.runtime_state)
        _require_bool(self.review_exchange_running, "review_exchange_running")
        _require_bool(
            self.validation_preflight_configured,
            "validation_preflight_configured",
        )

    @property
    def requests_review_exchange(self) -> bool:
        return RequestedAction.CREATE_PR in self.requested_actions

    @property
    def requests_push(self) -> bool:
        return RequestedAction.PUSH_BRANCH in self.requested_actions


@dataclass(frozen=True)
class CompletionFinalizationPlan:
    """Decision plus the reason for logging/tests."""

    decision: CompletionFinalizationDecision
    reason: str

    def __post_init__(self) -> None:
        _require_finalization_decision(self.decision)
        _require_non_empty_string(self.reason, "reason")


def decide_completion_finalization(
    command: CompletionFinalizationCommand,
) -> CompletionFinalizationPlan:
    """Return the next safe finalization step for a completion record.

    Precedence:

    1. Non-completed outcomes do not publish or review-exchange; process them.
    2. An already-running review exchange wins over dirty/publish preconditions.
    3. A timed-out visible session with an in-flight exchange is terminal.
    4. Dirty preflight runs only when validation is configured and push is requested.
    5. Otherwise the normal completion processor may continue.
    """
    if command.outcome is not CompletionOutcome.COMPLETED:
        return CompletionFinalizationPlan(
            decision=CompletionFinalizationDecision.PROCESS,
            reason="non-completed outcome does not require publish finalization",
        )

    if command.requests_review_exchange and command.review_exchange_running:
        if command.runtime_state is CompletionRuntimeState.TIMED_OUT:
            return CompletionFinalizationPlan(
                decision=CompletionFinalizationDecision.TERMINAL_REVIEW_EXCHANGE_TIMEOUT,
                reason="visible session timed out while review exchange is running",
            )
        return CompletionFinalizationPlan(
            decision=CompletionFinalizationDecision.DEFER_REVIEW_EXCHANGE,
            reason="review exchange is already running",
        )

    if command.requests_push and command.validation_preflight_configured:
        return CompletionFinalizationPlan(
            decision=CompletionFinalizationDecision.RUN_DIRTY_PREFLIGHT,
            reason="push requested with validation preflight configured",
        )

    return CompletionFinalizationPlan(
        decision=CompletionFinalizationDecision.PROCESS,
        reason="no finalization precondition blocks processing",
    )


def _require_positive_issue_number(issue_number: object) -> None:
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError("issue_number must be a positive integer")


def _require_session_name(session_name: object) -> None:
    if session_name is not None and (not isinstance(session_name, str) or not session_name):
        raise ValueError("session_name must be a non-empty string or None")


def _require_requested_actions(actions: object) -> None:
    if not isinstance(actions, tuple):
        raise TypeError("requested_actions must be a tuple")
    for action in actions:
        if not isinstance(action, RequestedAction):
            raise TypeError("requested_actions must contain RequestedAction values")


def _require_completion_outcome(outcome: object) -> None:
    if not isinstance(outcome, CompletionOutcome):
        raise TypeError("outcome must be a CompletionOutcome")


def _require_runtime_state(runtime_state: object) -> None:
    if not isinstance(runtime_state, CompletionRuntimeState):
        raise TypeError("runtime_state must be a CompletionRuntimeState")


def _require_bool(value: object, field_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be bool")


def _require_finalization_decision(decision: object) -> None:
    if not isinstance(decision, CompletionFinalizationDecision):
        raise TypeError("decision must be a CompletionFinalizationDecision")


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
