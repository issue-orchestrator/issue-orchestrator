"""Shared completion-processing result types."""

from dataclasses import dataclass

ERROR_PREFIX_PUSH = "push_branch"
ERROR_PREFIX_CREATE_PR = "create_pr"
ERROR_PREFIX_PUBLISH_BLOCKED = "publish_blocked"
REVIEW_EXCHANGE_ERROR_PREFIX = "review_exchange:"


@dataclass
class ProcessingResult:
    """Result of processing a completion record."""

    success: bool
    message: str
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

    @classmethod
    def for_review_exchange_deferred(cls) -> "ProcessingResult":
        """Typed constructor for the async review-exchange deferral result."""
        return cls(
            success=True,
            message="Review exchange running in background; will resume on next tick",
            completion_record_path=None,
            review_exchange_deferred=True,
        )
