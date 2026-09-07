"""One boundary joins filesystem allocation to durable issue-run ownership."""

from ..domain.issue_run_allocation import IssueExchangeRunAllocation, IssueRunAllocation
from ..domain.issue_run_evidence import IssueRunRecord, RunTerminalBinding
from ..domain.review_exchange_run import ReviewExchangeRun
from ..domain.session_key import SessionKey, TaskKind
from ..domain.session_run import SessionRunAssets, RunContainedFile
from ..ports.issue_run_evidence import IssueRunLedger
from ..ports.issue_run_allocator import IssueRunRoleConfiguration
from ..ports.session_output import SessionOutput
from ..ports.working_copy import WorkingCopy
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable


class IssueRunAllocationService:
    def __init__(
        self,
        output: SessionOutput,
        ledger: IssueRunLedger,
        working_copy: WorkingCopy,
        *,
        configuration: IssueRunRoleConfiguration,
    ) -> None:
        self._working_copy = working_copy
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
            request.issue_number, request.session_key, run, request.agent_label, request.terminal_id
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
            None,
        )
        return run

    def _record(
        self,
        issue_number: int,
        key: SessionKey,
        run: SessionRunAssets,
        agent_label: str,
        terminal_id: str | None,
    ) -> None:
        status = self._working_copy.get_branch_status(run.worktree_path)
        if status is None or not status.branch or status.branch == "HEAD":
            raise IssueRunEvidenceUnavailable("Run allocation requires an attached branch")
        self._ledger.record_run(
            issue_number,
            IssueRunRecord(
                session_key=key,
                run=run,
                recorded_at=run.started_at,
                branch_name=status.branch,
                terminal_binding=RunTerminalBinding(terminal_id),
                agent_label=agent_label,
                completion_task=TaskKind.TECH_LEAD
                if agent_label == self._configuration.tech_lead_review_agent
                else key.task,
            ),
        )
