"""Port interfaces for the issue orchestrator hexagonal architecture.

This package contains the protocol definitions (interfaces) that define the
boundaries between the application core and external adapters. Following the
ports and adapters (hexagonal architecture) pattern, these protocols allow
the core business logic to remain independent of external dependencies.

The ports are organized by domain responsibility:
- IssueRepository: Access to issue data
- LabelManager: Label management operations
- PRRepository: Pull request operations
- SessionStore: Session persistence

Usage:
    from issue_orchestrator.ports import IssueRepository, LabelManager

    def process_issues(repo: IssueRepository, labels: LabelManager):
        issues = repo.list_issues(state="open")
        for issue in issues:
            if not labels.has_label(issue.number, "processed"):
                # Process issue...
                labels.add_label(issue.number, "processed")
"""

from issue_orchestrator.ports.issue_repository import IssueRepository
from issue_orchestrator.ports.label_manager import LabelManager
from issue_orchestrator.ports.pr_repository import PRInfo, PRRepository
from issue_orchestrator.ports.session_store import SessionStore

__all__ = [
    "IssueRepository",
    "LabelManager",
    "PRInfo",
    "PRRepository",
    "SessionStore",
]
