"""Bounded manual execution under the retry owner's real submission lifetime."""

from collections.abc import Callable
import logging

from ..domain.completion_processing import ProcessingResult
from ..domain.manual_publication import ManualPublicationResult
from ..domain.publish_retry import PublishRetryLocators
from ..domain.validated_head_publication import (
    SupersededStage,
    compose_publication_outcome,
    superseded_outcome,
)
from ..ports.manual_publication import ManualPublicationPreparation
from ..ports.validated_head_publication import ValidatedHeadExecutor


logger = logging.getLogger(__name__)


class ManualCompletionPublisher:
    def __init__(self, preparation: ManualPublicationPreparation, executor: ValidatedHeadExecutor):
        self._preparation = preparation
        self._executor = executor

    def publish(
        self, locators: PublishRetryLocators, issue_title: str,
        is_current: Callable[[], bool],
    ) -> ManualPublicationResult:
        prepared = self._preparation.prepare_manual_publication(locators, issue_title)
        if isinstance(prepared, ProcessingResult):
            return ManualPublicationResult(prepared, None)
        if not is_current():
            publication = superseded_outcome(SupersededStage.BEFORE_BRANCH_WRITE, None)
        else:
            branch = self._executor.push_validated_head(prepared.command)
            if branch.at_target and not is_current():
                publication = superseded_outcome(SupersededStage.BETWEEN_STEPS, branch)
            else:
                pr = self._executor.ensure_pull_request(prepared.command) if branch.at_target else None
                publication = compose_publication_outcome(branch, pr)
        if not is_current():
            # Preserve every observed effect for the retry owner's tombstone drain.
            # There is no invented "superseded after PR" stage in the executor algebra.
            processing = ProcessingResult(
                success=False, message="Manual publication submission was abandoned",
                pr_url=publication.pr_url, intake_receipt=prepared.receipt,
            )
        else:
            try:
                processing = self._preparation.settle_manual_publication(prepared, publication)
            except Exception as exc:
                # Settlement can fail after an accepted remote effect. Return those
                # facts to the owner so reset cleanup never loses the late PR.
                logger.exception("Manual publication settlement failed for issue=%s", locators.issue_number)
                processing = ProcessingResult(
                    success=False, message=f"Manual publication settlement failed: {exc}",
                    pr_url=publication.pr_url, intake_receipt=prepared.receipt,
                )
        return ManualPublicationResult(processing.with_processing_policy(prepared.processing_policy), publication, prepared.agent_label)
