"""Prepared retained publication, without manual-retry locators or a fabricated run."""

from dataclasses import dataclass

from .prepared_completion import PreparedCompletionEvidence
from .publication_workspace import PublicationWorkspace
from .registered_completion import CompletionProcessingPolicy
from .validated_head_publication import PublishValidatedHeadCommand
from .validated_work import ReviewDisposition


@dataclass(frozen=True, slots=True)
class PreparedRecoveryPublication:
    command: PublishValidatedHeadCommand
    workspace: PublicationWorkspace
    completion: PreparedCompletionEvidence
    processing_policy: CompletionProcessingPolicy
    review_disposition: ReviewDisposition

    def __post_init__(self) -> None:
        self.command.require_disposition_binding(self.workspace.record_id)
        if self.command.source_workspace != self.workspace.checkout:
            raise ValueError("recovery publication names another workspace")
        if self.completion.validation.head_sha != self.command.target_head_sha:
            raise ValueError("recovery publication differs from attested validation")
