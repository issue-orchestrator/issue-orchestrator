"""Fresh issue/runtime admission and disposable workspace preparation under a claim."""

from dataclasses import dataclass
from functools import partial

from ..domain.completion_processing import ProcessingResult
from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.recovery_publication import PreparedRecoveryPublication
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.publication_workspace import PublicationWorkspaces
from ..ports.recovery_issue_reader import RecoveryIssueReader, RecoveryIssueReadError
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_store import ValidatedWorkStore
from .retained_completion_preparation import RetainedCompletionPreparation
from .review_exchange_lifecycle import OtherRuntimeActivity


@dataclass(frozen=True, slots=True)
class ReadyRecoveryPublication:
    publication: PreparedRecoveryPublication
    issue_title: str


class ClaimedRecoveryPreparation:
    def __init__(self, *, repo_slug: str, store: ValidatedWorkStore,
                 effects: ValidatedWorkEffectAuthority, issues: RecoveryIssueReader,
                 runtime: OtherRuntimeActivity, gate: IssueDispositionMutationGate,
                 workspaces: PublicationWorkspaces, preparation: RetainedCompletionPreparation,
                 pause_label: str) -> None:
        self._repo, self._store, self._effects = repo_slug, store, effects
        self._issues, self._runtime, self._gate = issues, runtime, gate
        self._workspaces, self._preparation, self._pause_label = workspaces, preparation, pause_label

    def prepare(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                request: RecoveryRecordRequest) -> ReadyRecoveryPublication | RecoveryAttemptPending:
        perform = partial(self._effects.perform, token, claim)
        record = perform(lambda: self._store.record_for_id(claim.record_id))
        refusal = request.refusal(record)
        if refusal is not None:
            return refusal
        key = record.disposition.key
        if key.repo_slug != self._repo:
            raise ValueError("recovery record belongs to another repository")
        try:
            issue = perform(lambda: self._issues.read(self._repo, key.issue_number))
        except RecoveryIssueReadError as error:
            return RecoveryAttemptPending(str(error), ValidatedWorkFailure.ISSUE_UNREADABLE)
        issue.require_identity(self._repo, key.issue_number)
        if not issue.permits_recovery(self._pause_label):
            return RecoveryAttemptPending("Issue is closed or paused; recovery remains retained")
        if perform(lambda: self._runtime.probe(key.issue_number)).busy:
            return RecoveryAttemptPending("Other issue runtime is active or unverifiable", ValidatedWorkFailure.RUNTIME_ACTIVE)
        with self._gate.try_acquire(self._repo, key.issue_number) as acquired:
            if acquired is IssueDispositionGateStatus.BUSY:
                return RecoveryAttemptPending("Issue disposition mutation is busy")
            workspace = perform(lambda: self._workspaces.prepare(record.current_evidence.admission))
        prepared = perform(lambda: self._preparation.prepare(record.current_evidence, workspace, issue.title))
        if isinstance(prepared, ProcessingResult):
            return RecoveryAttemptPending(prepared.message)
        return ReadyRecoveryPublication(prepared, issue.title)
