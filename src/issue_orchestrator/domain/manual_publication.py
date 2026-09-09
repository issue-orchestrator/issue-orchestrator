"""Manual publication retains trusted preparation and exact remote stage facts."""

from dataclasses import dataclass

from .completion_intake import CompletionIntakeReceipt
from .completion_processing import ProcessingResult
from .registered_completion import CompletionProcessingPolicy
from .models import CompletionRecord, RequestedAction
from .review_exchange import ReviewExchangeOutcome
from .session_run import SessionRunAssets, RunContainedFile
from .validated_work import PublishValidatedHeadStatus
from .validated_head_publication import PublishValidatedHeadCommand, PublishValidatedHeadOutcome


@dataclass(frozen=True, slots=True)
class PreparedManualPublication:
    command: PublishValidatedHeadCommand
    receipt: CompletionIntakeReceipt
    run: SessionRunAssets
    completion_artifact: RunContainedFile
    record: CompletionRecord
    issue_title: str
    processing_policy: CompletionProcessingPolicy
    label_target: int
    actions_taken: tuple[str, ...]
    remaining_actions: tuple[RequestedAction, ...]
    exchange_mode: str | None
    exchange_result: ReviewExchangeOutcome | None
    review_exchange_completed: bool
    review_exchange_halted: bool

    @property
    def agent_label(self) -> str:
        label = self.processing_policy.agent_label
        if label is None:
            raise ValueError("manual preparation requires an allocated agent label")
        return label

    def __post_init__(self) -> None:
        if self.command.source_workspace != self.run.worktree_path:
            raise ValueError("manual command must use the original run workspace")
        if any(action in {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}
               for action in self.remaining_actions):
            raise ValueError("remaining manual actions cannot repeat publication")
        if not self.agent_label.startswith("agent:"):
            raise ValueError("manual preparation requires an allocated agent label")


@dataclass(frozen=True, slots=True)
class ManualPublicationResult:
    processing: ProcessingResult
    publication: PublishValidatedHeadOutcome | None
    agent_label: str | None = None

    def __post_init__(self) -> None:
        if self.processing.success and not self.processing.is_non_terminal:
            if self.publication is None or self.publication.status not in {
                PublishValidatedHeadStatus.PUBLISHED, PublishValidatedHeadStatus.ALREADY_AT_TARGET,
            }:
                raise ValueError("manual success requires a successful exact publication")
            if not self.publication.pr_url or self.processing.pr_url != self.publication.pr_url:
                raise ValueError("manual success must preserve the observed PR identity")

    @property
    def attributable_pr_number(self) -> int | None:
        if self.publication is None:
            return None
        return self.publication.attributable_pr_number
