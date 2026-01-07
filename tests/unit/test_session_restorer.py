"""Tests for SessionRestorer - session recovery after orchestrator restart.

These tests verify the behavior of restoring session tracking after restart:
- Session restoration from discovered running sessions
- Handling of orphaned sessions (no worktree found)
- Error recovery during restoration
- Validation of restored session state

Tests use mock adapters at port boundaries, not internal patches.
"""

import logging
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.session_restorer import SessionRestorer
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.session_runner import DiscoveredSession


class MockRepositoryHost:
    """Mock RepositoryHost for testing SessionRestorer.

    Implements only the methods used by SessionRestorer:
    - get_issue: Returns issue by number
    - remove_label: Tracks calls for orphaned issue cleanup
    """

    def __init__(self):
        self.issues: dict[int, Issue] = {}
        self.remove_label_calls: list[tuple[int, str]] = []
        self.remove_label_error: Exception | None = None

    def get_issue(self, issue_number: int) -> Issue | None:
        """Return issue from test data."""
        return self.issues.get(issue_number)

    def remove_label(self, issue_number: int, label: str) -> None:
        """Track label removal calls, optionally raising configured error."""
        self.remove_label_calls.append((issue_number, label))
        if self.remove_label_error is not None:
            raise self.remove_label_error


class MockWorkingCopy:
    """Mock WorkingCopy for testing SessionRestorer.

    Implements only the methods used by SessionRestorer:
    - get_current_branch: Returns branch name for worktree
    """

    def __init__(self):
        self.branches: dict[Path, str] = {}

    def get_current_branch(self, worktree: Path) -> str | None:
        """Return configured branch name for worktree."""
        return self.branches.get(worktree)


def make_discovered_session(
    issue_number: int,
    tab_name: str | None = None,
    is_review: bool = False,
) -> DiscoveredSession:
    """Create a DiscoveredSession for testing."""
    if tab_name is None:
        if is_review:
            tab_name = f"#100 Review PR #{issue_number}"
        else:
            tab_name = f"#{issue_number} Some task"
    return DiscoveredSession(
        issue_number=issue_number,
        tab_name=tab_name,
        is_review=is_review,
    )


def make_config(
    agents: dict[str, AgentConfig] | None = None,
    repo: str = "test/repo",
) -> Config:
    """Create a Config with the given agents."""
    config = Config()
    config.repo = repo
    if agents:
        config.agents = agents
    return config


def make_agent_config(
    tmp_path: Path,
) -> AgentConfig:
    """Create an AgentConfig for testing."""
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt,
    )


class TestRestoreSessionsBasic:
    """Tests for basic session restoration behavior."""

    def test_restores_code_session_with_worktree_and_issue(self, tmp_path):
        """A discovered code session with matching worktree and issue is restored."""
        # Setup: create worktree directory
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-test-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Act
        discovered = [make_discovered_session(123, is_review=False)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Assert
        assert len(restored) == 1
        session = restored[0]
        assert session.issue.number == 123
        assert session.terminal_id == "issue-123"
        assert session.worktree_path == worktree
        assert session.branch_name == "123-test-branch"
        assert session.key.task == TaskKind.CODE

    def test_restores_review_session_with_pr_number_from_tab_name(self, tmp_path):
        """A discovered review session extracts PR number from tab name."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(
            number=100,
            title="Original issue",
            labels=["agent:reviewer"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-feature-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Tab name format: "#<issue> Review PR #<pr>"
        discovered = [make_discovered_session(100, tab_name="#100 Review PR #456", is_review=True)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        session = restored[0]
        assert session.terminal_id == "review-456"  # PR number from tab name
        assert session.key.task == TaskKind.REVIEW

    def test_skips_already_tracked_sessions(self, tmp_path):
        """Sessions that are already tracked are not restored again."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Create an already-tracked session
        existing_session = MagicMock(spec=Session)
        existing_session.terminal_id = "issue-123"

        discovered = [make_discovered_session(123)]
        restored = restorer.restore_sessions(discovered, already_tracked=[existing_session])

        # Session is already tracked, so nothing restored
        assert len(restored) == 0

    def test_skips_duplicates_within_discovered_sessions(self, tmp_path):
        """If same session appears multiple times in discovered, only restore once."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
        )

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Same issue discovered twice
        discovered = [
            make_discovered_session(123, tab_name="#123 First tab"),
            make_discovered_session(123, tab_name="#123 Second tab"),
        ]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Only first one is restored; second is skipped as duplicate
        assert len(restored) == 1


class TestOrphanedSessionHandling:
    """Tests for handling sessions without corresponding worktrees."""

    def test_cleans_up_orphaned_session_without_worktree(self, tmp_path, caplog):
        """Sessions without worktrees are cleaned up (in-progress label removed)."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # Note: NO worktree created for issue 123

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        working_copy = MockWorkingCopy()

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session not restored
        assert len(restored) == 0

        # In-progress label removed from orphaned issue
        assert (123, "in-progress") in repo_host.remove_label_calls

    def test_handles_remove_label_failure_gracefully(self, tmp_path, caplog):
        """If removing label from orphaned issue fails, log warning but continue."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # No worktree for issue 123

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.remove_label_error = Exception("API error")
        working_copy = MockWorkingCopy()

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Still no session restored
        assert len(restored) == 0

        # But we attempted the cleanup
        assert (123, "in-progress") in repo_host.remove_label_calls

        # Warning logged
        assert "Failed to cleanup orphaned issue" in caplog.text


class TestErrorRecovery:
    """Tests for error handling during session restoration."""

    def test_continues_after_exception_restoring_single_session(self, tmp_path, caplog):
        """If one session fails to restore, continue with others."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree_200 = tmp_path / "repo-200"
        worktree_200.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # Only issue 200 exists; issue 100 will trigger cleanup path
        repo_host.issues[200] = Issue(number=200, title="Good issue", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree_200] = "200-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [
            make_discovered_session(100),  # Will fail - no worktree
            make_discovered_session(200),  # Will succeed
        ]

        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Only the successful session restored
        assert len(restored) == 1
        assert restored[0].issue.number == 200

    def test_exception_during_restore_is_logged(self, tmp_path, caplog):
        """Exceptions during single session restore are logged and continue."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        # Create a repo host that throws on get_issue
        class FailingRepoHost(MockRepositoryHost):
            def get_issue(self, issue_number: int) -> Issue | None:
                raise RuntimeError("Simulated API failure")

        repo_host = FailingRepoHost()
        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.ERROR):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No sessions restored due to exception
        assert len(restored) == 0

        # Exception logged
        assert "Failed to restore session for issue #123" in caplog.text


class TestStateValidation:
    """Tests for state validation during restoration."""

    def test_skips_session_without_agent_config(self, tmp_path, caplog):
        """Sessions without available agent config are treated as orphaned.

        When there are no agents configured, _find_worktree cannot find any
        worktrees (since it iterates over agent repo_roots). This results in
        the session being treated as orphaned and cleaned up.
        """
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        # No agents configured - means no worktrees can be found
        config = make_config(agents={})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=[])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No session restored - no agent config available means session skipped
        assert len(restored) == 0
        assert "No agent config available" in caplog.text

    def test_skips_session_without_repo_config(self, tmp_path, caplog):
        """Sessions without repo in config are skipped."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config}, repo=None)
        config.repo = None  # No repo configured
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        # No session restored
        assert len(restored) == 0
        assert "No repo configured" in caplog.text

    def test_creates_minimal_issue_when_issue_not_found(self, tmp_path):
        """When issue not found in repo, creates minimal issue object."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # No issue 123 in repo - get_issue returns None

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123, tab_name="#123 My task")]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session still restored with minimal issue
        assert len(restored) == 1
        session = restored[0]
        assert session.issue.number == 123
        assert session.issue.title == "123 My task"  # Tab name with # stripped

    def test_uses_fallback_agent_config_when_issue_has_no_agent_label(self, tmp_path):
        """Uses first available agent config when issue has no agent type label."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        # Issue with no agent: label
        repo_host.issues[123] = Issue(number=123, title="Test", labels=[])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        # Session restored with fallback agent config
        assert len(restored) == 1
        assert restored[0].agent_config == agent_config


class TestBranchNameResolution:
    """Tests for branch name resolution from worktrees."""

    def test_uses_unknown_branch_when_git_fails(self, tmp_path, caplog):
        """When working copy fails to get branch, uses 'unknown' as fallback."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        # No branch configured for worktree - returns None

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        with caplog.at_level(logging.WARNING):
            restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].branch_name == "unknown"
        assert "Failed to get branch name" in caplog.text


class TestWorktreeFinding:
    """Tests for worktree discovery logic."""

    def test_finds_worktree_in_sibling_directory(self, tmp_path):
        """Worktree is found as sibling of repo_root with issue number suffix."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # Worktree is sibling: repo-123
        worktree = tmp_path / "repo-123"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:web": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[123] = Issue(number=123, title="Test", labels=["agent:web"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "123-feature"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(123)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].worktree_path == worktree

class TestReviewSessionSpecifics:
    """Tests specific to review session restoration."""

    def test_review_session_uses_issue_number_when_pr_not_in_tab(self, tmp_path):
        """Review session falls back to issue number if PR number not in tab name."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(number=100, title="Test", labels=["agent:reviewer"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        # Tab name without PR number pattern
        discovered = [make_discovered_session(100, tab_name="#100 Review Something", is_review=True)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        # Falls back to issue number as PR number
        assert restored[0].terminal_id == "review-100"

    def test_review_session_has_correct_task_kind(self, tmp_path):
        """Review sessions have TaskKind.REVIEW in their session key."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        worktree = tmp_path / "repo-100"
        worktree.mkdir()

        agent_config = make_agent_config(tmp_path)
        config = make_config(agents={"agent:reviewer": agent_config})
        config.repo_root = repo_root

        repo_host = MockRepositoryHost()
        repo_host.issues[100] = Issue(number=100, title="Test", labels=["agent:reviewer"])

        working_copy = MockWorkingCopy()
        working_copy.branches[worktree] = "100-branch"

        restorer = SessionRestorer(config, repo_host, working_copy)

        discovered = [make_discovered_session(100, is_review=True)]
        restored = restorer.restore_sessions(discovered, already_tracked=[])

        assert len(restored) == 1
        assert restored[0].key.task == TaskKind.REVIEW
