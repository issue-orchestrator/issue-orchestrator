"""Explicit allocation wiring for bounded component tests."""

from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.control.completion_review_exchange import CompletionReviewExchange
from issue_orchestrator.control.issue_run_allocator import IssueRunAllocationService
from issue_orchestrator.control.session_launcher import SessionLauncher
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

    def recorded_runs(self, issue_number):
        return tuple(record for number, record in self.records.values() if number == issue_number)


def allocation_for(output):
    return IssueRunAllocationService(output, MemoryIssueRunLedger())


def make_completion_processor(*args, **kwargs) -> CompletionProcessor:
    output = kwargs["session_output"] if "session_output" in kwargs else args[3]
    kwargs.setdefault("issue_run_allocator", allocation_for(output))
    return CompletionProcessor(*args, **kwargs)


def make_completion_review_exchange(**kwargs) -> CompletionReviewExchange:
    kwargs.setdefault("issue_run_allocator", allocation_for(kwargs["session_output"]))
    return CompletionReviewExchange(**kwargs)


def make_session_launcher(*args, **kwargs) -> SessionLauncher:
    output = kwargs["session_output"] if "session_output" in kwargs else args[8]
    ledger = kwargs.pop("issue_run_ledger", MemoryIssueRunLedger())
    kwargs.setdefault("issue_run_allocator", IssueRunAllocationService(output, ledger))
    return SessionLauncher(*args, **kwargs)


def make_worktree_context(**kwargs):
    from issue_orchestrator.control.worktree_context import WorktreeContext
    from issue_orchestrator.domain.issue_key import FakeIssueKey
    from issue_orchestrator.domain.session_key import SessionKey, TaskKind
    kwargs.setdefault("run_allocator", allocation_for(kwargs["session_output"]))
    kwargs.setdefault("session_key", SessionKey(FakeIssueKey(str(kwargs["issue_number"])), TaskKind.CODE))
    return WorktreeContext.create(**kwargs)
