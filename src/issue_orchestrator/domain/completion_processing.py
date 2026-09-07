"""Typed outcome of completion processing across owner boundaries."""

from dataclasses import dataclass, replace

from .completion_intake import CompletionIntakeError
from .registered_completion import CompletionProcessingPolicy


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
    processing_policy: CompletionProcessingPolicy | None = None
    failure_kind: str | None = None
    pr_url: str | None = None
    actions_taken: list[str] | None = None
    diagnostic_path: str | None = None
    completion_record_path: str | None = None
    errors: list[str] | None = None
    review_exchange_completed: bool = False
    review_exchange_halted: bool = False
    # True when the review exchange is running asynchronously and completion
    # processing for this record must retry on a future tick. Callers must NOT
    # treat the session as terminated while this flag is set — the completion
    # record is intentionally left on disk so the next observation re-enters
    # the pipeline.
    review_exchange_deferred: bool = False
    # True when a post-review validation failure was preserved and rerouted
    # back into coder rework via the review-exchange path. Callers should keep
    # the session running but still surface validation-failure evidence.
    validation_failed_rerouted: bool = False

    def require_processing_policy(self) -> CompletionProcessingPolicy:
        """A processed result cannot be reclassified as an unprocessed session."""
        if self.processing_policy is None:
            raise CompletionIntakeError("completion processing did not establish a role policy")
        return self.processing_policy

    def with_processing_policy(self, policy: CompletionProcessingPolicy) -> "ProcessingResult":
        """Bind the selected invocation policy to every terminal/deferred outcome."""
        return replace(self, processing_policy=policy)

    @classmethod
    def for_intake_refusal(cls, error: CompletionIntakeError) -> "ProcessingResult":
        """Untrusted intake confers no role authority on terminal handling."""
        return cls(
            success=False, message=str(error), errors=[str(error)],
            processing_policy=CompletionProcessingPolicy(None, None),
        )

    @classmethod
    def for_review_exchange_deferred(cls) -> "ProcessingResult":
        """Typed constructor for the async review-exchange deferral result."""
        return cls(
            success=True,
            message="Review exchange running in background; will resume on next tick",
            completion_record_path=None,
            review_exchange_deferred=True,
        )

    @property
    def is_non_terminal(self) -> bool:
        """True when completion has NOT finished for this record.

        The review exchange is running in the background (``review_exchange_deferred``)
        and/or a post-review validation failure was rerouted into coder rework
        (``validation_failed_rerouted``). The live session path leaves such a
        completion pending — ``SessionController`` maps it to ``SessionStatus.RUNNING``
        and resumes publishing on a later tick. Other consumers of a
        ``ProcessingResult`` (e.g. retry-publish reconciliation) must not treat a
        non-terminal result as terminal success, or they would clear recovery
        state before publish actually completes.
        """
        return self.review_exchange_deferred or self.validation_failed_rerouted
