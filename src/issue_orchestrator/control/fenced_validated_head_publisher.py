"""Record-bound two-step publication under the caller's retained execution lease."""

from ..domain.validated_head_publication import (
    PublishValidatedHeadCommand,
    PublishValidatedHeadOutcome,
    SupersededStage,
    compose_publication_outcome,
    superseded_outcome,
)
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken, ValidatedWorkClaimLost
from ..ports.validated_head_publication import ValidatedHeadExecutor
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority


class FencedValidatedHeadPublisher:
    def __init__(
        self, executor: ValidatedHeadExecutor, authority: ValidatedWorkEffectAuthority
    ) -> None:
        self._executor = executor
        self._authority = authority

    def publish(
        self,
        token: RecordExecutionToken,
        claim: ValidatedWorkClaim,
        command: PublishValidatedHeadCommand,
    ) -> PublishValidatedHeadOutcome:
        """No claim acquisition, release, attempt writes, or unfenced combined call.

        Unknown authority raises rather than fabricating supersession or a
        remote failure. The disposition caller retains its outcome-less durable
        attempt for reconciliation, including a branch write that may have landed.
        """
        command.require_disposition_binding(claim.record_id)
        try:
            branch = self._authority.perform(
                token, claim, lambda: self._executor.push_validated_head(command)
            )
        except ValidatedWorkClaimLost:
            return superseded_outcome(SupersededStage.BEFORE_BRANCH_WRITE, None)
        if not branch.at_target:
            return compose_publication_outcome(branch, None)
        try:
            pr = self._authority.perform(
                token, claim, lambda: self._executor.ensure_pull_request(command)
            )
        except ValidatedWorkClaimLost:
            return superseded_outcome(SupersededStage.BETWEEN_STEPS, branch)
        return compose_publication_outcome(branch, pr)
