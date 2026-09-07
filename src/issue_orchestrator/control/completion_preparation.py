"""Prepared completion phases shared by live execution and manual publication."""

from dataclasses import dataclass
import json
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.completion_intake import CompletionIntakeReceipt
from ..domain.session_run import SessionRunAssets
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


def record_from_prepared_evidence(
    evidence: PreparedCompletionEvidence, receipt: CompletionIntakeReceipt | None, run: SessionRunAssets,
) -> CompletionRecord:
    """Consume only the immutable bytes bound to this exact invocation."""
    if evidence.entry.receipt != receipt or evidence.run.run != run:
        raise ValueError("prepared completion does not bind this receipt and run")
    return CompletionRecord.from_dict(json.loads(evidence.completion_bytes))
