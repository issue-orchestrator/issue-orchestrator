"""Test builders for creating domain objects with sensible defaults.

Usage:
    # Simple issue with defaults
    issue = IssueBuilder().build()

    # Customized issue
    issue = IssueBuilder().with_number(42).with_title("Fix bug").with_agent("agent:web").build()

    # Session with issue
    session = SessionBuilder().for_issue(issue).build()

    # Pre-configured mock adapter scenarios
    adapter = MockGitHubAdapterScenarios.with_open_issues([1, 2, 3])
    adapter = MockGitHubAdapterScenarios.with_pr_for_issue(123, "https://github.com/test/repo/pull/456")
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionKey,
    TaskKind,
    PendingReview,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports.pull_request_tracker import PRInfo


@dataclass
class IssueBuilder:
    """Builder for creating Issue objects with sensible defaults.

    Example:
        issue = IssueBuilder().with_number(42).with_agent("agent:web").build()
    """

    _number: int = 1
    _title: str = "Test issue"
    _labels: list[str] = field(default_factory=list)
    _body: Optional[str] = None
    _state: str = "open"
    _milestone: Optional[str] = None

    def with_number(self, number: int) -> "IssueBuilder":
        """Set the issue number."""
        self._number = number
        return self

    def with_title(self, title: str) -> "IssueBuilder":
        """Set the issue title."""
        self._title = title
        return self

    def with_labels(self, *labels: str) -> "IssueBuilder":
        """Set the issue labels."""
        self._labels = list(labels)
        return self

    def with_agent(self, agent_label: str) -> "IssueBuilder":
        """Add an agent label (e.g., 'agent:web')."""
        if agent_label not in self._labels:
            self._labels = list(self._labels) + [agent_label]
        return self

    def with_body(self, body: str) -> "IssueBuilder":
        """Set the issue body."""
        self._body = body
        return self

    def with_state(self, state: str) -> "IssueBuilder":
        """Set the issue state ('open' or 'closed')."""
        self._state = state
        return self

    def with_milestone(self, milestone: str) -> "IssueBuilder":
        """Set the issue milestone."""
        self._milestone = milestone
        return self

    def build(self) -> Issue:
        """Build the Issue object."""
        return Issue(
            number=self._number,
            title=self._title,
            labels=self._labels,
            body=self._body,
            state=self._state,
            milestone=self._milestone,
        )


@dataclass
class SessionBuilder:
    """Builder for creating Session objects with sensible defaults.

    Example:
        session = SessionBuilder().for_issue(issue).with_terminal_id("issue-123").build()
    """

    _issue: Optional[Issue] = None
    _task: TaskKind = TaskKind.CODE
    _terminal_id: Optional[str] = None
    _worktree_path: Path = field(default_factory=lambda: Path("/tmp/test-worktree"))
    _branch_name: str = "test-branch"
    _completion_path: str = ".issue-orchestrator/completion.json"
    _agent_label: Optional[str] = None

    def for_issue(self, issue: Issue) -> "SessionBuilder":
        """Set the issue this session is for."""
        self._issue = issue
        if self._terminal_id is None:
            self._terminal_id = f"issue-{issue.number}"
        if self._agent_label is None:
            self._agent_label = issue.agent_type
        return self

    def with_task(self, task: TaskKind) -> "SessionBuilder":
        """Set the task type (CODE, REVIEW, REWORK, TRIAGE)."""
        self._task = task
        return self

    def with_terminal_id(self, terminal_id: str) -> "SessionBuilder":
        """Set the terminal session ID."""
        self._terminal_id = terminal_id
        return self

    def with_worktree(self, path: Path) -> "SessionBuilder":
        """Set the worktree path."""
        self._worktree_path = path
        return self

    def with_branch(self, branch: str) -> "SessionBuilder":
        """Set the branch name."""
        self._branch_name = branch
        return self

    def with_agent(self, agent_label: str) -> "SessionBuilder":
        """Set the agent label."""
        self._agent_label = agent_label
        return self

    def build(self) -> Session:
        """Build the Session object."""
        from unittest.mock import MagicMock

        if self._issue is None:
            self._issue = IssueBuilder().with_agent("agent:test").build()

        issue_key = GitHubIssueKey(repo="test/repo", external_id=str(self._issue.number))
        session_key = SessionKey(issue=issue_key, task=self._task)

        # Create a minimal mock agent config
        agent_config = MagicMock()
        agent_config.timeout_minutes = 60
        agent_config.provider_args = {"permission_mode": "bypassPermissions"}
        agent_config.effective_permission_mode = "bypassPermissions"
        agent_config.command = "claude -p --model {model} {initial_prompt}"

        return Session(
            key=session_key,
            issue=self._issue,
            agent_config=agent_config,
            terminal_id=self._terminal_id or f"issue-{self._issue.number}",
            worktree_path=self._worktree_path,
            branch_name=self._branch_name,
            completion_path=self._completion_path,
            agent_label=self._agent_label,
        )


@dataclass
class PendingReviewBuilder:
    """Builder for creating PendingReview objects.

    Example:
        review = PendingReviewBuilder().for_issue(123).with_pr(456).build()
    """

    _issue_number: int = 1
    _pr_number: int = 100
    _pr_url: str = "https://github.com/test/repo/pull/100"
    _branch_name: str = "1-test-branch"
    _agent_label: Optional[str] = None

    def for_issue(self, issue_number: int) -> "PendingReviewBuilder":
        """Set the issue number."""
        self._issue_number = issue_number
        self._branch_name = f"{issue_number}-test-branch"
        return self

    def with_pr(self, pr_number: int, pr_url: Optional[str] = None) -> "PendingReviewBuilder":
        """Set the PR number and optionally URL."""
        self._pr_number = pr_number
        self._pr_url = pr_url or f"https://github.com/test/repo/pull/{pr_number}"
        return self

    def with_branch(self, branch: str) -> "PendingReviewBuilder":
        """Set the branch name."""
        self._branch_name = branch
        return self

    def with_agent(self, agent_label: str) -> "PendingReviewBuilder":
        """Set the agent label."""
        self._agent_label = agent_label
        return self

    def build(self) -> PendingReview:
        """Build the PendingReview object."""
        issue_key = GitHubIssueKey(repo="test/repo", external_id=str(self._issue_number))
        return PendingReview(
            issue_key=issue_key,
            pr_number=self._pr_number,
            pr_url=self._pr_url,
            branch_name=self._branch_name,
            _issue_number=self._issue_number,
            agent_label=self._agent_label,
        )


class MockGitHubAdapterScenarios:
    """Factory methods for common MockGitHubAdapter test scenarios.

    Usage:
        adapter = MockGitHubAdapterScenarios.with_open_issues([1, 2, 3])
        adapter = MockGitHubAdapterScenarios.with_pr_for_issue(123, pr_url)
    """

    @staticmethod
    def empty() -> "MockGitHubAdapter":
        """Create an empty adapter with no data."""
        from tests.conftest import MockGitHubAdapter
        return MockGitHubAdapter()

    @staticmethod
    def with_open_issues(
        issue_numbers: list[int],
        agent_label: str = "agent:web",
    ) -> "MockGitHubAdapter":
        """Create adapter with specified open issues.

        Args:
            issue_numbers: List of issue numbers to create
            agent_label: Agent label for all issues

        Returns:
            Configured MockGitHubAdapter
        """
        from tests.conftest import MockGitHubAdapter

        adapter = MockGitHubAdapter()
        for num in issue_numbers:
            adapter.issues.append(
                IssueBuilder()
                .with_number(num)
                .with_title(f"Issue {num}")
                .with_agent(agent_label)
                .build()
            )
        return adapter

    @staticmethod
    def with_pr_for_issue(
        issue_number: int,
        pr_url: str,
        pr_number: Optional[int] = None,
        branch: Optional[str] = None,
    ) -> "MockGitHubAdapter":
        """Create adapter with a PR linked to an issue.

        Args:
            issue_number: The issue number
            pr_url: URL of the PR
            pr_number: PR number (derived from URL if not provided)
            branch: Branch name (derived from issue if not provided)

        Returns:
            Configured MockGitHubAdapter
        """
        from tests.conftest import MockGitHubAdapter, PRInfo

        adapter = MockGitHubAdapter()

        # Extract PR number from URL if not provided
        if pr_number is None:
            pr_number = int(pr_url.split("/")[-1])

        # Default branch name
        if branch is None:
            branch = f"{issue_number}-feature"

        # Add the issue
        adapter.issues.append(
            IssueBuilder()
            .with_number(issue_number)
            .with_title(f"Issue {issue_number}")
            .with_agent("agent:web")
            .build()
        )

        # Add the PR
        adapter.prs[branch] = [
            PRInfo(
                number=pr_number,
                title=f"PR for issue {issue_number}",
                url=pr_url,
                branch=branch,
                body="",
                state="open",
                labels=[],
            )
        ]

        return adapter

    @staticmethod
    def with_blocked_issue(
        issue_number: int,
        blocked_by: list[int],
    ) -> "MockGitHubAdapter":
        """Create adapter with an issue blocked by dependencies.

        Args:
            issue_number: The blocked issue number
            blocked_by: List of issue numbers that block this issue

        Returns:
            Configured MockGitHubAdapter
        """
        from tests.conftest import MockGitHubAdapter

        adapter = MockGitHubAdapter()

        # Build dependency body
        deps_body = "Depends on: " + ", ".join(f"#{n}" for n in blocked_by)

        # Add blocked issue
        adapter.issues.append(
            IssueBuilder()
            .with_number(issue_number)
            .with_title(f"Issue {issue_number}")
            .with_agent("agent:web")
            .with_body(deps_body)
            .build()
        )

        # Add blocking issues (closed)
        for blocking_num in blocked_by:
            adapter.issues.append(
                IssueBuilder()
                .with_number(blocking_num)
                .with_title(f"Blocking issue {blocking_num}")
                .with_state("closed")
                .build()
            )

        return adapter


# Re-export for convenience
from tests.conftest import MockGitHubAdapter, MockEventSink

__all__ = [
    "IssueBuilder",
    "SessionBuilder",
    "PendingReviewBuilder",
    "MockGitHubAdapterScenarios",
    "MockGitHubAdapter",
    "MockEventSink",
    "PRInfo",
]
