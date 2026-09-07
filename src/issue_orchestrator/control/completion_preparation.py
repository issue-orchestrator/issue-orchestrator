"""Prepared completion phases shared by live execution and manual publication."""

from dataclasses import dataclass
from .stack_base import StackBaseDecision

from ..domain.models import CompletionRecord
from ..domain.review_exchange import ReviewExchangeOutcome
from .review_publish_pipeline import PublishPipelinePlan


@dataclass(frozen=True, slots=True)
class PreparedActionPlan:
    plan: PublishPipelinePlan
    exchange_mode: str | None
    exchange_result: ReviewExchangeOutcome | None
    review_exchange_completed: bool
    halted: bool


@dataclass(frozen=True, slots=True)
class PreparedCompletion:
    record: CompletionRecord
    session_name: str | None
    agent_label: str | None
    branch: str | None
    preserved_completion_path: str | None
    actions: PreparedActionPlan


@dataclass(frozen=True, slots=True)
class PreparedPullRequest:
    title: str
    body: str
    base_branch: str
    stack_decision: StackBaseDecision | None
    exchange_mode: str | None
