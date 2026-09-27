"""Construct the single issue lifecycle bundle from explicit live owner objects."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from ..control.review_exchange_lifecycle import CoreIssueRuntimeOwners, IssueRuntimeLifecycleOwners, IssuePublishRetryRuntime
from ..control.fact_gatherer import FactGatherer
from ..control.issue_run_evidence import IssueRunEvidenceService
from ..control.published_review_custody import BranchPullRequestReader, PublishedReviewCustody
from ..control.validated_work_preservation import ValidatedWorkPreservationService
from ..domain.issue_run_evidence import IssueRunRecord, RunTerminalBinding
from ..ports.issue_run_evidence import IssueRunLedger
from ..ports.event_sink import EventSink
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.working_copy import WorkingCopy
from ..ports.persistent_exchange_pair_registry import PersistentExchangePairRegistry
from ..control.background_job_supervisor import BackgroundJobSupervisor
from ..control.session_manager import SessionManager
from ..domain.models import OrchestratorState
from ..infra.worktree_base import resolve_base_branch
from ..control.validated_head_base import PullRequestBaseRef
from .bootstrap_validated_work import ValidatedWorkAdmissionOwners

if TYPE_CHECKING:
    from ..control.stack_publish_gate import StackBaseGate
    from ..infra.config import Config


def build_issue_runtime(*, state: OrchestratorState, ledger: IssueRunLedger,
        intake: CompletionIntakeRuntime, validated_work: ValidatedWorkAdmissionOwners,
        working_copy: WorkingCopy, sessions: SessionManager,
        pair_registry: PersistentExchangePairRegistry | None,
        supervisor: BackgroundJobSupervisor | None,
        publish_recovery: IssuePublishRetryRuntime, events: EventSink,
        pull_requests: BranchPullRequestReader, stuck_sweep: FactGatherer | None,
        base_ref: Callable[[int, Path], str | None]) -> IssueRuntimeLifecycleOwners:
    def live_runs(issue_number: int) -> tuple[IssueRunRecord, ...]:
        return tuple(IssueRunRecord(session.key, session.run_assets, session.run_assets.started_at, session.branch_name, RunTerminalBinding(session.terminal_id))
            for session in state.active_sessions if session.issue.number == issue_number)

    evidence = IssueRunEvidenceService(ledger, live_runs=live_runs, now=lambda: datetime.now(timezone.utc).isoformat())
    preservation = ValidatedWorkPreservationService(intake=intake, store=validated_work.store,
        custody=validated_work.custody, repair=validated_work.repair, working_copy=working_copy,
        observer=validated_work.capture_observer, base_ref=base_ref)
    published_review = PublishedReviewCustody(validated_work.store, pull_requests)
    if stuck_sweep is not None:
        # The sweep asks the very owner the reset gate enforces (#7293).
        stuck_sweep.published_review = published_review
    return IssueRuntimeLifecycleOwners(CoreIssueRuntimeOwners(sessions, state.active_sessions,
        pair_registry, supervisor, publish_recovery), preservation, evidence, events, published_review)


def remote_base_ref(
    config: "Config", default_branch: Callable[[Path], str], stack_gate: "StackBaseGate | None",
) -> PullRequestBaseRef:
    """The base a validated head must be ahead of to be work (#7347).

    Resolved lazily, per capture: a stack successor's predecessor branch from
    the stack base gate, otherwise the branch worktrees are created from.
    """
    def resolve_default() -> str:
        return resolve_base_branch(
            config.repo_root,
            config_override=config.worktree_base_branch_override,
            default_branch_resolver=default_branch,
        ).branch
    return PullRequestBaseRef(resolve_default, stack_gate)
