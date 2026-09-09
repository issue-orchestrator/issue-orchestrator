"""Append-before-effect publication, resumed under the disposition owner's lease."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial

from ..domain.published_work_finalization import PublishedWorkTarget
from ..domain.recovery_attempt import (
    RecoveryAttemptPending, RecoveryAttemptPlan, publication_can_finalize, target_from_verification,
)
from ..domain.recovery_publication import PreparedRecoveryPublication
from ..domain.validated_head_publication import PublishValidatedHeadCommand
from ..domain.validated_work import PublishValidatedHeadStatus
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot
from ..domain.validated_work_execution import RecordExecutionToken
from ..ports.publication_verifier import PublicationVerifier
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_store import ValidatedWorkStore
from .fenced_validated_head_publisher import FencedValidatedHeadPublisher


class RecoveryPublicationAttempt:
    def __init__(self, *, store: ValidatedWorkStore, effects: ValidatedWorkEffectAuthority,
                 publisher: FencedValidatedHeadPublisher, verifier: PublicationVerifier,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._store, self._effects = store, effects
        self._publisher, self._verifier = publisher, verifier
        self._clock = clock

    def advance(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                prepared: PreparedRecoveryPublication, *,
                approved: ValidatedWorkAuthoritySnapshot | None = None,
                ) -> PublishedWorkTarget | RecoveryAttemptPending:
        """One synchronous stage; caller keeps execution and claim through finalization.

        A lost acknowledgement preserves the prior attempt. A successor may
        append a reconciliation attempt within the store's existing budget;
        a durable success uses read-only confirmation and never invokes push.
        """
        perform = partial(self._effects.perform, token, claim)
        prepared.command.require_disposition_binding(claim.record_id)
        record = perform(lambda: self._store.record_for_id(claim.record_id))
        attempts = perform(lambda: self._store.publish_attempts(claim.record_id))
        plan = RecoveryAttemptPlan.for_record(prepared, record, attempts)
        if plan.resume_finalization:
            return self._confirm(token, claim, prepared, plan.command)
        fresh = perform(lambda: self._verifier.before_publication(plan.command, plan.phase))
        if not fresh.verified:
            return RecoveryAttemptPending(fresh.message, fresh.failure)
        attempt = perform(lambda: self._store.begin_publish_attempt(
            claim, expected_attempt_no=plan.previous_attempt_no,
            target_head_sha=plan.command.target_head_sha,
            expected_remote_head=plan.command.expected_remote_head_sha or "",
            phase=plan.phase, started_at=self._clock().isoformat(), authority=approved,
        ))
        if attempt is None:
            return RecoveryAttemptPending("Store refused publication attempt")
        outcome = self._publisher.publish(token, claim, plan.command)
        if outcome.status is PublishValidatedHeadStatus.SUPERSEDED:
            return RecoveryAttemptPending(outcome.message)
        recorded = perform(lambda: self._store.record_attempt_outcome(
            claim, attempt, outcome=outcome.status, failure=outcome.failure,
            finished_at=self._clock().isoformat(),
        ))
        if not recorded:
            return RecoveryAttemptPending("Publication outcome awaits durable reconciliation")
        if not publication_can_finalize(outcome):
            return RecoveryAttemptPending(outcome.message, outcome.failure)
        return self._confirm(token, claim, prepared, plan.command)

    def _confirm(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                 prepared: PreparedRecoveryPublication, command: PublishValidatedHeadCommand,
                 ) -> PublishedWorkTarget | RecoveryAttemptPending:
        perform = partial(self._effects.perform, token, claim)
        observed = perform(lambda: self._verifier.confirm_target(command))
        target = target_from_verification(prepared, observed)
        if isinstance(target, RecoveryAttemptPending):
            return target
        recorded = perform(lambda: self._store.record_pr_number(claim, pr_number=target.pr_number))
        if not recorded:
            return RecoveryAttemptPending("Verified PR identity awaits durable recording")
        # Re-read using the durably recorded identity; marker-only adoption is
        # no longer enough if another PR appears during the acknowledgement.
        confirmed = perform(lambda: self._verifier.confirm_target(replace(command, pr_number=target.pr_number)))
        return target_from_verification(prepared, confirmed)
