"""One boundary joins filesystem allocation to durable issue-run ownership."""

from ..domain.issue_run_allocation import IssueExchangeRunAllocation, IssueRunAllocation
from ..domain.issue_run_evidence import IssueRunRecord
from ..domain.review_exchange_run import ReviewExchangeRun
from ..domain.session_key import SessionKey, TaskKind
from ..domain.session_run import SessionRunAssets, RunContainedFile
from ..ports.issue_run_evidence import IssueRunLedger
from ..ports.issue_run_allocator import IssueRunRoleConfiguration
from ..ports.session_output import SessionOutput


class IssueRunAllocationService:
    def __init__(
        self,
        output: SessionOutput,
        ledger: IssueRunLedger,
        *,
        configuration: IssueRunRoleConfiguration,
    ) -> None:
        self._output = output
        self._ledger = ledger
        self._configuration = configuration

    def submission_capability_file(self, run: SessionRunAssets) -> RunContainedFile:
        return self._ledger.submission_capability_file(run)

    def submission_capability(self, run: SessionRunAssets) -> str:
        return self._ledger.submission_capability(run)

    def allocate(self, request: IssueRunAllocation) -> SessionRunAssets:
        run = self._output.start_run(
            worktree_path=request.worktree_path,
            session_name=request.session_name,
            issue_number=request.issue_number,
            agent_label=request.agent_label,
            backend=request.backend,
            claude_log_dir=request.claude_log_dir,
            orchestrator_log=request.orchestrator_log,
            retention_tier=request.retention_tier,
            retention_days=request.retention_days,
            retention_pinned=request.retention_pinned,
        )
        self._record(
            request.issue_number, request.session_key, run, request.agent_label
        )
        return run

    def allocate_exchange(
        self, request: IssueExchangeRunAllocation
    ) -> ReviewExchangeRun:
        run = self._output.start_review_exchange_run(
            request.worktree_path,
            issue_number=request.issue_number,
            parent_session_name=request.parent_session_name,
            agent_label=request.agent_label,
        )
        self._record(
            request.issue_number,
            request.session_key,
            run.session_run,
            request.agent_label,
        )
        return run

    def _record(
        self,
        issue_number: int,
        key: SessionKey,
        run: SessionRunAssets,
        agent_label: str,
    ) -> None:
        self._ledger.record_run(
            issue_number,
            IssueRunRecord(
                session_key=key,
                run=run,
                recorded_at=run.started_at,
                agent_label=agent_label,
                completion_task=TaskKind.TECH_LEAD
                if agent_label == self._configuration.tech_lead_review_agent
                else key.task,
            ),
        )
