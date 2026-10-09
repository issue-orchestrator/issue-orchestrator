"""Own operator abandonment from exact authority through block reprojection."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager

from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from ..domain.recovery_block import RecoveryMutationBusy
from ..domain.validated_work import ResolutionKind
from ..domain.validated_work_execution import RecordExecutionBusy
from ..domain.validated_work_commands import (
    AbandonAllOutcome,
    AbandonStatus,
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
)
from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.recovery_block import RecoveryBlockIssueReconciler
from ..ports.validated_work_abandonment import ValidatedWorkAbandonmentStore
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner


class OperatorValidatedWorkAbandonment:
    """Serialize exact durable abandonment and trigger aggregate repair."""

    def __init__(
        self,
        *,
        repo_slug: str,
        store: ValidatedWorkAbandonmentStore,
        execution: ValidatedWorkExecutionOwner,
        gate: IssueDispositionMutationGate,
        blocks: RecoveryBlockIssueReconciler,
        events: EventSink,
    ) -> None:
        self._repo = repo_slug
        self._store = store
        self._execution = execution
        self._gate = gate
        self._blocks = blocks
        self._events = events

    def abandon(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        authority = command.authority
        if authority.repo_slug != self._repo:
            raise ValueError("abandonment names another repository")
        lease = self._execution.try_enter(authority.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return self._busy("The retained-work record is already executing")
        with lease as token:
            self._execution.require_active(token, authority.record_id)
            try:
                with self._hold_issue(authority.issue_number):
                    outcome = self._store.abandon_if_current(command)
            except RecoveryMutationBusy:
                return self._busy("The retained-work issue is already changing")
            if outcome.status is not AbandonStatus.ABANDONED:
                return outcome
            self._publish_abandoned(command)
            self._blocks.reconcile_issue_block(
                authority.issue_number
            ).require_reconciled()
            return outcome

    def abandon_all(
        self, commands: tuple[AbandonValidatedWorkCommand, ...]
    ) -> AbandonAllOutcome:
        """Abandon several records of ONE issue atomically (#9092).

        Every record's execution lease and the issue's mutation gate are held
        across one store transaction, so either every record resolves or none
        does, and the aggregate block is reprojected once afterwards.
        """
        if not commands:
            raise ValueError("a batch abandonment names at least one record")
        if any(command.authority.repo_slug != self._repo for command in commands):
            raise ValueError("abandonment names another repository")
        issues = {command.authority.issue_number for command in commands}
        if len(issues) != 1:
            raise ValueError("a batch abandonment names the records of one issue")
        (issue_number,) = issues
        first = commands[0].authority.record_id
        with ExitStack() as leases:
            for command in commands:
                record_id = command.authority.record_id
                lease = self._execution.try_enter(record_id)
                if isinstance(lease, RecordExecutionBusy):
                    return self._busy_batch(
                        record_id, "The retained-work record is already executing"
                    )
                token = leases.enter_context(lease)
                self._execution.require_active(token, record_id)
            try:
                with self._hold_issue(issue_number):
                    outcome = self._store.abandon_all_if_current(commands)
            except RecoveryMutationBusy:
                return self._busy_batch(
                    first, "The retained-work issue is already changing"
                )
            if not outcome.committed:
                return outcome
            for command in commands:
                self._publish_abandoned(command)
            self._blocks.reconcile_issue_block(issue_number).require_reconciled()
            return outcome

    def _publish_abandoned(self, command: AbandonValidatedWorkCommand) -> None:
        authority = command.authority
        self._events.publish(
            make_trace_event(
                EventName.VALIDATED_WORK_ABANDONED,
                {
                    "issue_number": authority.issue_number,
                    "record_id": authority.record_id,
                    "evidence_id": authority.evidence_id,
                    "actor": command.actor,
                    "reason": command.reason,
                    "resolution_kind": ResolutionKind.OPERATOR_ABANDONED.value,
                },
            )
        )

    @classmethod
    def _busy_batch(cls, record_id: str, message: str) -> AbandonAllOutcome:
        return AbandonAllOutcome((), cls._busy(message), record_id)

    @staticmethod
    def _busy(message: str) -> AbandonValidatedWorkOutcome:
        return AbandonValidatedWorkOutcome(
            AbandonStatus.BUSY, None, (), None, message
        )

    @contextmanager
    def _hold_issue(self, issue_number: int) -> Generator[None]:
        with self._gate.try_acquire(self._repo, issue_number) as status:
            if status is IssueDispositionGateStatus.BUSY:
                raise RecoveryMutationBusy("issue mutation busy")
            yield
