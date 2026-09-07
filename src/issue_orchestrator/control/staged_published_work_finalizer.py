"""Durable routing-before-release finalization; deliberately not runtime-wired."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from ..domain.published_work_finalization import (
    FinalizationOutcome,
    FinalizationCheckpoint,
    FinalizationStatus as Status,
    PublishedWorkFinalizationRequest,
    RecoveryBlockReleaseRequest,
    RecoveryBlockReleaseStatus,
)
from ..domain.validated_work import (
    FinalizationPhase as Phase,
    ValidatedWorkFailure,
    require_text,
)
from ..domain.validated_work_execution import (
    ValidatedWorkClaimLost,
    ValidatedWorkAuthorityUnavailable,
)
from ..ports.fresh_issue_reader import FreshIssueReader, FreshIssueReadError
from ..ports.published_work_finalization import (
    AggregateRecoveryBlockOwner,
    FinalizationPhaseRecorder,
)
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from .actions import ActionResult, AddLabelAction
from .published_review_state import PublishedReviewState
from .retry_review_routing import RetryReviewPolicy


class RoutingLabelApplier(Protocol):
    def apply(self, action: AddLabelAction) -> ActionResult: ...


class _RetryFinalization(RuntimeError):
    """A refused phase or aggregate release leaves durable progress unchanged."""


class _RoutingFailed(RuntimeError):
    pass


@dataclass
class _Progress:
    phase: Phase
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def outcome(
        self,
        request: PublishedWorkFinalizationRequest,
        status: Status,
        message: str,
        failure: ValidatedWorkFailure | None = None,
    ) -> FinalizationOutcome:
        return FinalizationOutcome(
            status,
            self.phase,
            request.target.review_disposition,
            tuple(self.added),
            tuple(self.removed),
            failure,
            message,
        )


class StagedPublishedWorkFinalizer:
    def __init__(
        self,
        *,
        effects: ValidatedWorkEffectAuthority,
        phases: FinalizationPhaseRecorder,
        recovery: AggregateRecoveryBlockOwner,
        fresh_issue_reader: FreshIssueReader,
        action_applier: RoutingLabelApplier,
        review_policy: RetryReviewPolicy,
        routing_label: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        require_text(routing_label, "routing_label")
        self._effects = effects
        self._phases = phases
        self._recovery = recovery
        self._reader = fresh_issue_reader
        self._applier = action_applier
        self._policy = review_policy
        self._routing_label = routing_label
        self._clock = clock
        self._state = PublishedReviewState(effects)

    def finalize(
        self, request: PublishedWorkFinalizationRequest
    ) -> FinalizationOutcome:
        request.require_routing_label(self._routing_label)
        progress = _Progress(request.resume_from)
        try:
            checkpoint = self._read_checkpoint(request)
            if checkpoint is None:
                raise _RetryFinalization("store refused finalization authority")
            progress.phase = checkpoint.phase  # durable truth overrides stale hints
            if checkpoint.failure is not None:
                return progress.outcome(
                    request, Status.FAILED, checkpoint.message, checkpoint.failure
                )
            if progress.phase is Phase.NOT_STARTED:
                self._write_routing(request, progress)
            self._replay(request)
            if progress.phase is Phase.NOT_STARTED:
                self._observe_routing(request)
                self._commit(request, progress, Phase.REVIEW_ROUTED)
            if progress.phase is Phase.REVIEW_ROUTED:
                self._release(request, progress)
                self._commit(request, progress, Phase.RECOVERY_CLEARED)
            # Even an advanced-phase, already-replayed invocation must still own
            # the claim before reporting success to its disposition caller.
            return self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: progress.outcome(
                    request, Status.FINALIZED, "published work finalized"
                ),
            )
        except _RoutingFailed as error:
            return self._fail_routing(request, progress, str(error))
        except (
            FreshIssueReadError,
            ValidatedWorkClaimLost,
            ValidatedWorkAuthorityUnavailable,
            _RetryFinalization,
        ) as error:
            return progress.outcome(request, Status.TRANSIENT, str(error))

    def _read_checkpoint(
        self, request: PublishedWorkFinalizationRequest
    ) -> FinalizationCheckpoint | None:
        try:
            return self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: self._phases.read_finalization_checkpoint(
                    request.claim, request.target
                ),
            )
        except (ValidatedWorkClaimLost, ValidatedWorkAuthorityUnavailable):
            raise
        except Exception as error:
            raise _RetryFinalization(f"phase read unavailable: {error}") from error

    def _write_routing(
        self,
        request: PublishedWorkFinalizationRequest,
        progress: _Progress,
    ) -> None:
        action = AddLabelAction(
            issue_number=request.target.key.issue_number,
            label=self._routing_label,
            reason="validated publication awaiting review or merge",
        )
        try:
            result = self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: self._applier.apply(action),
            )
        except (
            FreshIssueReadError,
            ValidatedWorkClaimLost,
            ValidatedWorkAuthorityUnavailable,
        ):
            raise
        except Exception as error:
            raise _RoutingFailed(str(error) or type(error).__name__) from error
        if not result.success:
            raise _RoutingFailed(result.error or "review routing label write failed")
        progress.added.append(self._routing_label)

    def _replay(self, request: PublishedWorkFinalizationRequest) -> None:
        candidate = self._policy.candidate(
            issue_number=request.target.key.issue_number,
            pr_number=request.target.pr_number,
            pr_url=request.target.pr_url,
            agent_label=request.agent_label,
            routing=request.routing,
        )
        self._state.replay(request, candidate, completed_at=self._clock())

    def _observe_routing(self, request: PublishedWorkFinalizationRequest) -> None:
        labels = self._effects.perform(
            request.execution_token,
            request.claim,
            lambda: self._reader.read_issue_labels(request.target.key.issue_number),
        )
        if self._routing_label not in labels:
            raise _RoutingFailed("review routing label was not observed after write")

    def _commit(
        self,
        request: PublishedWorkFinalizationRequest,
        progress: _Progress,
        phase: Phase,
    ) -> None:
        try:
            recorded = self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: self._phases.record_finalization_phase(
                    request.claim,
                    phase=phase,
                    recorded_at=self._clock().isoformat(),
                ),
            )
        except (ValidatedWorkClaimLost, ValidatedWorkAuthorityUnavailable):
            raise
        except Exception as error:
            # An acknowledgement can be lost AFTER the durable commit. Read it
            # back under the same authority, but never continue the next stage
            # during this interrupted invocation.
            durable = self._read_checkpoint(request)
            if durable is not None:
                progress.phase = durable.phase
            raise _RetryFinalization(f"phase write unavailable: {error}") from error
        if not recorded:
            raise _RetryFinalization("phase write refused; no further stage authorized")
        progress.phase = phase

    def _release(
        self,
        request: PublishedWorkFinalizationRequest,
        progress: _Progress,
    ) -> None:
        release = RecoveryBlockReleaseRequest(
            request.execution_token,
            request.claim,
            request.target,
            progress.phase,
            request.recovery_label,
            request.observed_blocking_labels,
        )
        try:
            result = self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: self._recovery.release_published_record(release),
            )
        except (
            FreshIssueReadError,
            ValidatedWorkClaimLost,
            ValidatedWorkAuthorityUnavailable,
        ):
            raise
        except Exception as error:
            raise _RetryFinalization(
                f"aggregate release unavailable: {error}"
            ) from error
        if result.record_id != request.claim.record_id:
            raise _RetryFinalization("aggregate release outcome names another record")
        progress.removed.extend(result.labels_removed)
        if result.status is RecoveryBlockReleaseStatus.REFUSED:
            raise _RetryFinalization(result.message)

    def _fail_routing(
        self,
        request: PublishedWorkFinalizationRequest,
        progress: _Progress,
        message: str,
    ) -> FinalizationOutcome:
        failure = ValidatedWorkFailure.REVIEW_ROUTING_FAILED
        try:
            recorded = self._effects.perform(
                request.execution_token,
                request.claim,
                lambda: self._phases.fail(
                    request.claim,
                    failure=failure,
                    reason=message,
                    failed_at=self._clock().isoformat(),
                ),
            )
        except Exception as error:
            return self._reconcile_failure(request, progress, error)
        if not recorded:
            return progress.outcome(request, Status.TRANSIENT, "failure write refused")
        return progress.outcome(request, Status.FAILED, message, failure)

    def _reconcile_failure(
        self,
        request: PublishedWorkFinalizationRequest,
        progress: _Progress,
        error: Exception,
    ) -> FinalizationOutcome:
        try:
            checkpoint = self._read_checkpoint(request)
        except (
            ValidatedWorkClaimLost,
            ValidatedWorkAuthorityUnavailable,
            _RetryFinalization,
        ):
            checkpoint = None
        if checkpoint is not None:
            progress.phase = checkpoint.phase
            if checkpoint.failure is not None:
                return progress.outcome(
                    request, Status.FAILED, checkpoint.message, checkpoint.failure
                )
        return progress.outcome(
            request, Status.TRANSIENT, f"failure write unavailable: {error}"
        )
