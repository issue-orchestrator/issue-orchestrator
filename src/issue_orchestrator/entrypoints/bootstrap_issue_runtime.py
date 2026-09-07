"""Construct the single issue lifecycle bundle from explicit live owner objects."""

from datetime import datetime, timezone
from ..control.review_exchange_lifecycle import CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners, IssuePublishRetryRuntime
from ..control.issue_run_evidence import IssueRunEvidenceService
from ..control.validated_work_preservation import ValidatedWorkPreservationService
from ..domain.issue_run_evidence import IssueRunRecord
from ..ports.issue_run_evidence import IssueRunLedger
from ..ports.event_sink import EventSink
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.working_copy import WorkingCopy
from ..ports.persistent_exchange_pair_registry import PersistentExchangePairRegistry
from ..control.background_job_supervisor import BackgroundJobSupervisor
from ..control.session_manager import SessionManager
from ..domain.models import OrchestratorState
from .bootstrap_validated_work import ValidatedWorkAdmissionOwners


def build_issue_runtime(*, state: OrchestratorState, ledger: IssueRunLedger,
        intake: CompletionIntakeRuntime, validated_work: ValidatedWorkAdmissionOwners,
        working_copy: WorkingCopy, sessions: SessionManager,
        pair_registry: PersistentExchangePairRegistry | None,
        supervisor: BackgroundJobSupervisor | None,
        publish_recovery: IssuePublishRetryRuntime, events: EventSink) -> IssueRuntimeLifecycleOwners:
    def live_runs(issue_number: int) -> tuple[IssueRunRecord, ...]:
        return tuple(IssueRunRecord(session.key, session.run_assets, session.run_assets.started_at, session.branch_name)
            for session in state.active_sessions if session.issue.number == issue_number)

    evidence = IssueRunEvidenceService(ledger, live_runs=live_runs, now=lambda: datetime.now(timezone.utc).isoformat())
    preservation = ValidatedWorkPreservationService(intake=intake, store=validated_work.store,
        custody=validated_work.custody, repair=validated_work.repair, working_copy=working_copy)
    return IssueRuntimeLifecycleOwners(CoreIssueRuntimeOwners(sessions, state.active_sessions,
        pair_registry, supervisor, publish_recovery), preservation, evidence, events)
