"""Queue audit functionality for debugging issue scheduling.

This module provides the single source of truth for determining why
issues are queued or skipped. Both the web UI and CLI audit command
should use this module.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.issue_tracker import IssueTracker

from .analysis import analyze_issue
from .config import Config
from . import labels as label_utils
from ..domain.dependencies import parse_dependencies
from ..ports.issue import Issue
from ..domain.models import OrchestratorState
from ..control.scheduler import Scheduler


class SkipReason(Enum):
    """Reasons why an issue might be skipped."""
    QUEUED = "queued"
    CLOSED = "closed"
    IN_PROGRESS = "in-progress label"
    HAS_OPEN_PR = "has open PR"
    HAS_BRANCH = "has branch (no PR)"
    BLOCKED = "blocked label"
    NEEDS_HUMAN = "needs-human label"
    IN_HISTORY = "in session history"
    NO_AGENT = "no matching agent label"
    ACTIVE_SESSION = "active session running"


@dataclass
class IssueAuditEntry:
    """Audit entry for a single issue."""
    issue: Issue
    status: SkipReason
    detail: Optional[str] = None

    def __str__(self) -> str:
        if self.status == SkipReason.QUEUED:
            return f"  #{self.issue.number} {self.issue.title} - QUEUED ({self.issue.agent_type})"
        else:
            detail_str = f" ({self.detail})" if self.detail else ""
            return f"  #{self.issue.number} {self.issue.title} - SKIP: {self.status.value}{detail_str}"


def fetch_all_issues(
    config: Config,
    issue_tracker: "IssueTracker",
) -> list[Issue]:
    """Fetch all issues for configured agents.

    This is the single source of truth for fetching issues.
    Used by both audit and web UI.

    Args:
        config: Configuration with agent labels and repo info.
        issue_tracker: IssueTracker port for fetching issues.

    Returns:
        Deduplicated list of issues sorted by number.
    """
    all_issues: list[Issue] = []
    milestones = config.get_filter_milestones()
    if not milestones:
        milestones = [None]

    for agent_label in config.agents.keys():
        labels = [agent_label]
        if config.filtering.label:
            labels.append(config.filtering.label)
        for milestone in milestones:
            fetched = issue_tracker.list_issues(
                labels=labels,
                milestone=milestone,
                limit=config.filtering.fetch_limit,
            )
            all_issues.extend(fetched)

    # Dedupe by issue number
    seen = set()
    unique_issues = []
    for issue in all_issues:
        if issue.number not in seen:
            seen.add(issue.number)
            unique_issues.append(issue)

    return unique_issues


def get_queue_issues(
    config: Config,
    state: Optional[OrchestratorState] = None,
    all_issues: Optional[list[Issue]] = None,
    issue_tracker: Optional["IssueTracker"] = None,
) -> list[Issue]:
    """Get issues that should be in the queue (ready to process).

    This is the single source of truth for queue filtering.
    Used by both audit and web UI.

    Args:
        config: Configuration.
        state: Optional orchestrator state for session/history filtering.
        all_issues: Optional pre-fetched issues (fetches if not provided).
        issue_tracker: Optional IssueTracker for fetching issues if all_issues not provided.

    Returns:
        List of issues ready for processing, sorted by priority.
    """
    if all_issues is None:
        if issue_tracker is None:
            raise ValueError("issue_tracker is required when all_issues is not provided")
        all_issues = fetch_all_issues(config, issue_tracker)

    # Get history and active issue numbers
    history_numbers = set()
    active_numbers = set()
    if state:
        history_numbers = {e.issue_number for e in state.session_history}
        active_numbers = {s.issue.number for s in state.active_sessions}

    # Use scheduler's filtering (same as run_loop uses)
    # Note: dependency checking is disabled here since we're just auditing
    scheduler = Scheduler(config)
    available, _ = scheduler.get_available_issues(all_issues, check_dependencies=False)

    # Filter out active and history items (same as web UI)
    queue_issues = [
        issue for issue in available
        if issue.number not in active_numbers and issue.number not in history_numbers
    ]

    # Sort by priority (same order as launching)
    return scheduler.sort_by_priority(queue_issues)


def audit_queue(
    config: Config,
    state: Optional[OrchestratorState] = None,
    issue_tracker: Optional["IssueTracker"] = None,
    issue_branches: Optional[dict[int, str]] = None,
) -> list[IssueAuditEntry]:
    """Audit all issues and explain why each is queued or skipped.

    Args:
        config: Configuration with agent labels and repo info.
        state: Optional orchestrator state for session history check.
        issue_tracker: IssueTracker for fetching issues.

    Returns:
        List of audit entries, one per issue.
    """
    if issue_tracker is None:
        raise ValueError("issue_tracker is required")

    entries = []

    # Get history issue numbers
    history_numbers = set()
    active_numbers = set()
    if state:
        history_numbers = {e.issue_number for e in state.session_history}
        active_numbers = {s.issue.number for s in state.active_sessions}

    if issue_branches is None:
        issue_branches = {}

    # Fetch all issues
    all_issues = fetch_all_issues(config, issue_tracker)

    # Sort by issue number for consistent output
    all_issues.sort(key=lambda i: i.number)

    # Audit each issue
    for issue in all_issues:
        entry = audit_issue(issue, config, history_numbers, active_numbers, issue_branches)
        entries.append(entry)

    return entries


def audit_issue(
    issue: Issue,
    config: Config,
    history_numbers: set[int],
    active_numbers: set[int],
    issue_branches: Optional[dict[int, str]] = None,
) -> IssueAuditEntry:
    """Determine why an issue is queued or skipped."""

    # Check if closed
    if issue.state == "closed":
        return IssueAuditEntry(issue, SkipReason.CLOSED)

    # Check for active session
    if issue.number in active_numbers:
        return IssueAuditEntry(issue, SkipReason.ACTIVE_SESSION)

    # Check labels - use centralized label_utils for blocking check
    label_in_progress = config.get_label_in_progress()
    label_needs_human = config.get_label_needs_human()

    if label_in_progress in issue.labels or "in-progress" in issue.labels:
        # Use analyze_issue to get accurate state (same logic as startup)
        if issue_branches is not None:
            state = analyze_issue(
                issue=issue,
                repo=config.repo,
                issue_branches=issue_branches,
                check_session_fn=lambda n: n in active_numbers,
            )
            if state.has_open_pr:
                return IssueAuditEntry(issue, SkipReason.HAS_OPEN_PR, f"PR pending review")
            elif state.has_partial_work:
                return IssueAuditEntry(issue, SkipReason.HAS_BRANCH, f"branch '{state.branch}' exists")
            elif state.is_orphaned_label:
                return IssueAuditEntry(issue, SkipReason.IN_PROGRESS, "orphaned - will be cleaned at startup")
        # Fallback if no branches info
        return IssueAuditEntry(issue, SkipReason.IN_PROGRESS, "work in progress")

    # Use centralized blocking check - catches blocked, blocked-failed, blocked-needs-human, etc.
    blocking_labels = label_utils.get_blocking_labels(issue.labels)
    if blocking_labels:
        # Return specific reason based on blocking label
        if label_utils.requires_human_any(issue.labels):
            return IssueAuditEntry(issue, SkipReason.NEEDS_HUMAN, f"label: {blocking_labels[0]}")
        return IssueAuditEntry(issue, SkipReason.BLOCKED, f"label: {blocking_labels[0]}")

    if label_needs_human in issue.labels or "needs-human" in issue.labels:
        return IssueAuditEntry(issue, SkipReason.NEEDS_HUMAN)

    # Check session history
    if issue.number in history_numbers:
        return IssueAuditEntry(issue, SkipReason.IN_HISTORY, "already processed this run")

    # Check for agent label
    if not issue.agent_type or issue.agent_type not in config.agents:
        return IssueAuditEntry(
            issue,
            SkipReason.NO_AGENT,
            f"has {issue.agent_type or 'no agent label'}"
        )

    # Issue is queued
    return IssueAuditEntry(issue, SkipReason.QUEUED)


def print_audit(entries: list[IssueAuditEntry], verbose: bool = False) -> None:
    """Print audit results to stdout.

    Args:
        entries: List of audit entries.
        verbose: If True, show all entries. If False, only show queued and skipped with reasons.
    """
    queued = [e for e in entries if e.status == SkipReason.QUEUED]
    skipped = [e for e in entries if e.status != SkipReason.QUEUED]

    print(f"\nQueue Audit: {len(queued)} queued, {len(skipped)} skipped\n")

    if queued:
        print("QUEUED:")
        for entry in queued:
            print(str(entry))
        print()

    if skipped:
        print("SKIPPED:")
        for entry in skipped:
            print(str(entry))
        print()


@dataclass
class IssueDependencyInfo:
    """Dependency information for a single issue (for web UI)."""

    issue_number: int
    has_dependencies: bool = False
    dependencies: list[tuple[int, str]] = field(default_factory=list)  # List of (issue_number, title)
    summary: str = ""  # Summary message for tooltip


def get_issue_dependencies(
    issues: list[Issue],
    config: Config,
) -> dict[int, IssueDependencyInfo]:
    """Get dependency info for a list of issues (for web UI display).

    This function parses dependencies from issue bodies and returns
    a mapping that can be used by the web UI to show warning icons
    and dependency lists.

    Note: This does NOT check if dependencies are satisfied - that
    would require GitHub API calls. It just extracts the declared
    dependencies for display purposes.

    Args:
        issues: List of issues to analyze.
        config: Configuration object.

    Returns:
        Dictionary mapping issue number to IssueDependencyInfo.
    """
    # Build a lookup of issue number -> title for dependencies
    issue_titles: dict[int, str] = {i.number: i.title for i in issues}

    result: dict[int, IssueDependencyInfo] = {}

    for issue in issues:
        if not issue.body:
            result[issue.number] = IssueDependencyInfo(issue_number=issue.number)
            continue

        # Parse dependencies from body
        deps = parse_dependencies(issue.body)

        if not deps:
            result[issue.number] = IssueDependencyInfo(issue_number=issue.number)
            continue

        # Build dependency list with titles
        dep_list = []
        for dep_num, dep_repo in deps:
            if dep_repo:
                # Cross-repo dependency
                title = f"{dep_repo}#{dep_num}"
            else:
                # Same-repo dependency - use title if available
                title = issue_titles.get(dep_num, f"Issue #{dep_num}")
            dep_list.append((dep_num, title))

        summary = f"Depends on: {', '.join(f'#{d[0]}' for d in dep_list)}"

        result[issue.number] = IssueDependencyInfo(
            issue_number=issue.number,
            has_dependencies=True,
            dependencies=dep_list,
            summary=summary,
        )

    return result
