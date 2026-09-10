"""Claimed refresh of transiently unavailable retained-work authority."""

from collections.abc import Callable
from datetime import datetime, timezone
from functools import partial

from ..domain.publication_remote import PublicationRemoteError
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_execution import RecordExecutionBusy
from ..domain.validated_work_capture import ValidatedWorkRemoteRequest
from ..domain.validated_work_remote_authority import (
    RemoteAuthorityRefreshRequest,
    refreshed_remote_authority,
)
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner
from ..ports.validated_work_store import ValidatedWorkStore


class RemoteAuthorityRefreshOperation:
    """Own one exact refresh from local lease through transactional settlement."""

    def __init__(
        self,
        *,
        execution: ValidatedWorkExecutionOwner,
        effects: ValidatedWorkEffectAuthority,
        store: ValidatedWorkStore,
        observer: ValidatedWorkCaptureObserver,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._execution = execution
        self._effects = effects
        self._store = store
        self._observer = observer
        self._clock = clock

    def run(self, request: RemoteAuthorityRefreshRequest) -> RecoveryAttemptPending:
        lease = self._execution.try_enter(request.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return RecoveryAttemptPending("Remote authority refresh is already executing")
        with lease as token:
            if not self._execution.relinquish(token):
                return RecoveryAttemptPending("Record awaits its reserved stop operation")
            record = self._store.record_for_id(request.record_id)
            refusal = request.refusal(record)
            if refusal is not None:
                return RecoveryAttemptPending(refusal)
            claim = self._store.acquire_claim(
                request.record_id,
                expected_states=frozenset({request.state}),
                evidence_id=request.evidence_id,
            )
            if claim is None:
                return RecoveryAttemptPending(
                    "Remote authority refresh belongs to another owner or changed"
                )
            self._execution.remember_claim(token, claim)
            try:
                perform = partial(self._effects.perform, token, claim)
                record = perform(lambda: self._store.record_for_id(request.record_id))
                refusal = request.refusal(record)
                if refusal is not None:
                    return RecoveryAttemptPending(refusal)
                key = record.disposition.key
                try:
                    facts = perform(
                        lambda: self._observer.observe(
                            ValidatedWorkRemoteRequest(
                                key.repo_slug, key.issue_number, key.branch_name
                            )
                        )
                    )
                except PublicationRemoteError as error:
                    return RecoveryAttemptPending(
                        str(error), ValidatedWorkFailure.REMOTE_UNREADABLE
                    )
                decision = refreshed_remote_authority(record, facts)
                updated = perform(
                    lambda: self._store.refresh_remote_authority(
                        claim,
                        request,
                        decision,
                        refreshed_at=self._clock().isoformat(),
                    )
                )
                if updated is None:
                    return RecoveryAttemptPending(
                        "Remote authority refresh changed before settlement"
                    )
                return RecoveryAttemptPending(decision.reason, decision.failure)
            finally:
                self._execution.relinquish(token)
