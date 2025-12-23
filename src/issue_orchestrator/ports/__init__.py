"""Port interfaces for the issue orchestrator hexagonal architecture.

This package contains the protocol definitions (interfaces) that define the
boundaries between the application core and external adapters. Following the
ports and adapters (hexagonal architecture) pattern, these protocols allow
the core business logic to remain independent of external dependencies.

Architecture naming conventions:
- Components that OBSERVE are named Observers (fact-gathering, non-authoritative)
- Components that DECIDE are named Controllers (policy, state transitions)
- Components that ACT are named Adapters (execution, external calls)

The ports are organized by domain responsibility:
- IssueRepository: Access to issue data (remote platform)
- LabelManager: Label management operations (remote platform)
- PRRepository: Pull request operations (remote platform)
- WorkingCopy: Local VCS operations (worktree context)
- SessionStore: Session persistence

Usage:
    from issue_orchestrator.ports import IssueRepository, LabelManager, WorkingCopy

    def process_issues(repo: IssueRepository, labels: LabelManager):
        issues = repo.list_issues(state="open")
        for issue in issues:
            if not labels.has_label(issue.number, "processed"):
                # Process issue...
                labels.add_label(issue.number, "processed")
"""

from issue_orchestrator.ports.issue_tracker import IssueTracker, IssueRepository
from issue_orchestrator.ports.label_set import LabelSet, LabelManager
from issue_orchestrator.ports.pull_request_tracker import PRInfo, PullRequestTracker, PRRepository
from issue_orchestrator.ports.session_store import SessionStore
from issue_orchestrator.ports.working_copy import (
    WorkingCopy,
    CommitInfo,
    BranchStatus,
    PushResult,
    RebaseResult,
)
from issue_orchestrator.ports.event_sink import EventSink, TraceEvent, NullEventSink
from issue_orchestrator.ports.session_runner import SessionRunner, NullSessionRunner
from issue_orchestrator.ports.repository_host import RepositoryHost

__all__ = [
    # Remote platform operations (new names)
    "IssueTracker",
    "LabelSet",
    "PRInfo",
    "PullRequestTracker",
    # Combined interface
    "RepositoryHost",
    # Backwards compatibility aliases
    "IssueRepository",
    "LabelManager",
    "PRRepository",
    # Local VCS operations
    "WorkingCopy",
    "CommitInfo",
    "BranchStatus",
    "PushResult",
    "RebaseResult",
    # Persistence
    "SessionStore",
    # Event emission (core -> external)
    "EventSink",
    "TraceEvent",
    "NullEventSink",
    # Terminal session management
    "SessionRunner",
    "NullSessionRunner",
]
