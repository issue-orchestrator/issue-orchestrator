"""Explicit allocation wiring for bounded component tests."""

from issue_orchestrator.infra.config import Config

from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.completion_review_exchange import CompletionReviewExchange
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.control.session_launcher import SessionLauncher
from unittest.mock import Mock
from issue_orchestrator.ports.completion_intake import CompletionIntakeRuntime

from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidenceUnavailable


class MemoryIssueRunLedger:
    def __init__(self):
        self.records = {}

    def record_run(self, issue_number, record):
        key = record.run.identity
        existing = self.records.get(key)
        if existing is not None and existing != (issue_number, record):
            raise IssueRunEvidenceUnavailable("Conflicting test ownership")
        self.records[key] = (issue_number, record)

    def submission_capability_file(self, run):
        from pathlib import Path
        from issue_orchestrator.domain.session_run import RunContainedFile

        self.submission_capability(run)
        root = Path("/private/tmp/test-owned-capabilities")
        return RunContainedFile(root, root / run.run_id)

    def submission_capability(self, run):
        if run.identity not in self.records:
            raise IssueRunEvidenceUnavailable("unallocated test run")
        return "test-run-capability"

    def issue_numbers(self):
        return tuple(sorted({number for number, _ in self.records.values()}))

    def recorded_runs(self, issue_number):
        return tuple(record for number, record in self.records.values() if number == issue_number)


def branch_working_copy():
    from issue_orchestrator.ports.working_copy import WorkingCopy, BranchStatus
    working_copy = Mock(spec=WorkingCopy)
    working_copy.get_branch_status.return_value = BranchStatus("feature", 0, 0, False, True)
    return working_copy


def allocation_for(output, configuration=None):
    return IssueRunAllocationService(output, MemoryIssueRunLedger(), branch_working_copy(), configuration=configuration if configuration is not None else Config(repo="example/repo"))


def make_completion_processor(*args, **kwargs) -> CompletionProcessor:
    output = kwargs["session_output"] if "session_output" in kwargs else args[3]
    kwargs.setdefault("issue_run_allocator", allocation_for(output, kwargs.get("config")))
    if "completion_intake" not in kwargs:
        # Legacy record-only tests do not exercise receipt authority. Any new
        # lifetime call must inject a real intake fixture, never a passing Mock.
        intake = Mock(spec=CompletionIntakeRuntime)
        for name in dir(CompletionIntakeRuntime):
            if not name.startswith("_") and callable(
                getattr(CompletionIntakeRuntime, name)
            ):
                getattr(intake, name).side_effect = AssertionError(
                    f"{name} requires an explicitly injected completion intake fixture"
                )
        kwargs["completion_intake"] = intake
    return CompletionProcessor(*args, **kwargs)


def make_completion_review_exchange(**kwargs) -> CompletionReviewExchange:
    kwargs.setdefault("issue_run_allocator", allocation_for(kwargs["session_output"], kwargs.get("config")))
    return CompletionReviewExchange(**kwargs)


def make_session_launcher(*args, **kwargs) -> SessionLauncher:
    output = kwargs["session_output"] if "session_output" in kwargs else args[8]
    ledger = kwargs.pop("issue_run_ledger", MemoryIssueRunLedger())
    configuration = kwargs["config"] if "config" in kwargs else args[0]
    kwargs.setdefault("issue_run_allocator", IssueRunAllocationService(output, ledger, branch_working_copy(), configuration=configuration))
    return SessionLauncher(*args, **kwargs)


def make_worktree_context(**kwargs):
    from issue_orchestrator.control.worktree_context import WorktreeContext
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.session_key import SessionKey, TaskKind
    kwargs.setdefault("run_allocator", allocation_for(kwargs["session_output"], kwargs.get("config")))
    kwargs.setdefault("session_key", SessionKey(FakeIssueKey(str(kwargs["issue_number"])), TaskKind.CODE))
    return WorktreeContext.create(**kwargs)
