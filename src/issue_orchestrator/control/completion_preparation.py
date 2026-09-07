"""Prepared completion phases shared by live execution and manual publication."""

from dataclasses import dataclass
import json
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.completion_intake import CompletionIntakeReceipt
from ..domain.session_run import SessionRunAssets
from ..domain.registered_completion import CompletionProcessingPolicy, RegisteredCompletion
from ..domain.completion_custody import normalized_completion_artifact
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
    processing_policy: CompletionProcessingPolicy
    branch: str | None
    preserved_completion_path: str | None
    actions: PreparedActionPlan

    @property
    def agent_label(self) -> str | None:
        return self.processing_policy.agent_label


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


def context_from_prepared_evidence(
    evidence: PreparedCompletionEvidence, receipt: CompletionIntakeReceipt | None, run: SessionRunAssets,
) -> RegisteredCompletion:
    """Keep immutable bytes and allocated role together at the policy boundary."""
    return RegisteredCompletion(
        record_from_prepared_evidence(evidence, receipt, run), evidence.role,
        normalized_completion_artifact(evidence.entry),
    )
