"""One issue-wide projection of retained recovery and human-block interests.

Admission must use the same issue gate. This owner never claims records, changes
their phases, publishes code, or removes another lifecycle's human-block cause.
"""

from collections.abc import Generator
from contextlib import contextmanager

from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from ..domain.published_work_finalization import (
    RecoveryBlockReleaseOutcome,
    RecoveryBlockReleaseRequest,
    RecoveryBlockReleaseStatus,
)
from ..domain.recovery_block import (
    RecoveryBlockInterest,
    RecoveryBlockPlan,
    RecoveryBlockReconcileOutcome,
    RecoveryBlockReconcileStatus as Status,
    RecoveryAdmissionDeferred,
    RecoveryMutationBusy,
)
from ..domain.validated_work_commands import ValidatedWorkDispositionBatch
from ..domain.validated_work_store import (
    AdmissionOutcome,
    EvidenceAdmission,
    EvidenceLookup,
    EvidenceRow,
)
from ..domain.validated_work_execution import (
    ValidatedWorkClaimLost,
    ValidatedWorkAuthorityUnavailable,
)
from ..ports.fresh_issue_reader import FreshIssueReader
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.published_work_finalization import FinalizationPhaseRecorder
from ..ports.recovery_block import RecoveryBlockStore
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.synchronous_effects import SynchronousEffectScope
from .recovery_labels import RecoveryLabels, RecoveryLabelApplier
from .captured_recovery_cleanup import CapturedRecoveryCleanup
from .label_manager import LabelManager
from .needs_human_block import (
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanCause,
    SharedNeedsHumanBlock,
    ValidatedWorkBlockSource,
)
from .recovery_block_effects import (
    ClaimedRecoveryProjectionEffects,
    IssueGateProjectionEffects,
)


class _RetryProjection(RuntimeError):
    pass


class AggregateRecoveryBlocks:
    def __init__(
        self,
        *,
        repo_slug: str,
        records: RecoveryBlockStore,
        admission: ValidatedWorkAdmissionStore,
        phases: FinalizationPhaseRecorder,
        authority: ValidatedWorkEffectAuthority,
        gate: IssueDispositionMutationGate,
        labels: LabelManager,
        reader: FreshIssueReader,
        applier: RecoveryLabelApplier,
        human_block: SharedNeedsHumanBlock,
    ) -> None:
        self._repo = repo_slug
        self._records = records
        self._admission = admission
        self._phases = phases
        self._authority = authority
        self._gate = gate
        self._labels = labels
        self._reader = reader
        self._label_writer = RecoveryLabels(reader, applier)
        self._cleanup = CapturedRecoveryCleanup(records, self._label_writer)
        self._human = human_block

    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome:
        """Rank/store through the injected owner, then observe the aggregate block.

        A busy gate makes no admission write. A failed projection leaves durable
        evidence intact and refuses acknowledgement so custody retries safely.
        Composition supplies the existing RankedEvidenceAdmission here.
        """
        key = admission.evidence.identity.key
        if key.repo_slug != self._repo:
            raise ValueError("admission names another repository")
        with self._hold_issue(key.issue_number):
            outcome = self._admission.admit(admission)
            try:
                snapshot = self._records.recovery_block_snapshot(
                    self._repo, key.issue_number
                )
                self._project(snapshot.plan(), IssueGateProjectionEffects())
            except Exception as error:
                raise RecoveryAdmissionDeferred(
                    f"durable admission awaits block reconciliation: {error}"
                ) from error
            return outcome

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._admission.for_issue(issue_number)

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._admission.has_unresolved_work(issue_number)

    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None:
        return self._admission.evidence_for_id(evidence_id)

    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]:
        return self._admission.retained_evidence(issue_number)

    def reconcile_issue_block(self, issue_number: int) -> RecoveryBlockReconcileOutcome:
        try:
            with self._hold_issue(issue_number):
                effects = IssueGateProjectionEffects()
                snapshot = self._records.recovery_block_snapshot(
                    self._repo, issue_number
                )
                return self._project(snapshot.plan(), effects)
        except RecoveryMutationBusy as error:
            return RecoveryBlockReconcileOutcome(Status.BUSY, (), str(error))
        except Exception as error:
            return RecoveryBlockReconcileOutcome(Status.RETRY, (), str(error))

    def release_published_record(
        self, request: RecoveryBlockReleaseRequest
    ) -> RecoveryBlockReleaseOutcome:
        record_id = request.claim.record_id
        try:
            request.require_context(self._repo, self._labels.recovery_pending)
            with self._hold_issue(request.target.key.issue_number):
                effects = ClaimedRecoveryProjectionEffects(self._authority, request)
                plan = self._admit_release(request, effects)
                result = self._project(plan, effects)
                return RecoveryBlockReleaseOutcome(
                    record_id,
                    RecoveryBlockReleaseStatus.RELEASED,
                    result.labels_removed,
                    result.message,
                )
        except (ValidatedWorkClaimLost, ValidatedWorkAuthorityUnavailable):
            raise
        except Exception as error:
            return RecoveryBlockReleaseOutcome(
                record_id, RecoveryBlockReleaseStatus.REFUSED, (), str(error)
            )

    def _admit_release(
        self,
        request: RecoveryBlockReleaseRequest,
        effects: SynchronousEffectScope,
    ) -> RecoveryBlockPlan:
        checkpoint = effects.perform(
            lambda: self._phases.read_finalization_checkpoint(
                request.claim, request.target
            )
        )
        snapshot = effects.perform(
            lambda: self._records.recovery_block_snapshot(
                self._repo, request.target.key.issue_number
            )
        )
        return snapshot.release_plan(request, checkpoint)

    def _project(
        self,
        plan: RecoveryBlockPlan,
        effects: SynchronousEffectScope,
    ) -> RecoveryBlockReconcileOutcome:
        if not plan.has_records:
            return RecoveryBlockReconcileOutcome(
                Status.RECONCILED, (), "no retained recovery interests"
            )
        issue = plan.issue_number
        removed: list[str] = []
        if plan.recovery_required:
            self._label_writer.set_presence(
                issue, self._labels.recovery_pending, effects, present=True
            )
        # Reassert failures before any source withdrawal, including after a
        # human removed the shared label and ended its previous generation.
        for item in plan.assert_human:
            self._human_effect(item, effects, acquire=True)
        recorded = self._human.with_effects(effects).recorded_sources(issue)
        for item in plan.withdraw_human:
            if ValidatedWorkBlockSource(item.disposition.record_id) in recorded:
                self._human_effect(item, effects, acquire=False)
        if not plan.recovery_required:
            removed.extend(self._cleanup.apply(
                plan,
                frozenset({self._labels.blocked_failed, self._labels.publish_failed}),
                effects,
            ))
            removed.extend(
                self._label_writer.set_presence(
                    issue, self._labels.recovery_pending, effects, present=False
                )
            )
        return RecoveryBlockReconcileOutcome(
            Status.RECONCILED, tuple(removed), "retained recovery interests projected"
        )

    def _human_effect(
        self,
        item: RecoveryBlockInterest,
        effects: SynchronousEffectScope,
        *,
        acquire: bool,
    ) -> None:
        request = HumanBlockRequest(
            item.disposition.key.issue_number,
            NeedsHumanCause.VALIDATED_WORK_DISPOSITION,
            item.disposition.reason,
            ValidatedWorkBlockSource(item.disposition.record_id),
        )
        scoped = self._human.with_effects(effects)
        operation = scoped.acquire if acquire else scoped.release
        result = operation(request)
        if result in {BlockOutcome.FAILED, BlockOutcome.UNGOVERNED}:
            raise _RetryProjection("human-block source projection did not commit")
        observed = effects.perform(
            lambda: self._reader.read_issue_labels(request.target)
        )
        if (self._labels.needs_human in observed) != (
            result is not BlockOutcome.CLEARED
        ):
            raise _RetryProjection("human-block projection was not observed")

    @contextmanager
    def _hold_issue(self, issue_number: int) -> Generator[None]:
        with self._gate.try_acquire(self._repo, issue_number) as status:
            if status is IssueDispositionGateStatus.BUSY:
                raise RecoveryMutationBusy("issue mutation busy")
            yield
