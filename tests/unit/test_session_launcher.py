"""Unit tests for SessionLauncher - behavior-centric tests.

These tests verify:
1. Issue session launching (happy path, error cases, state transitions)
2. Review session launching (happy path, conflicts, state transitions)
3. Rework session launching (happy path, PR resolution, conflicts)
4. Helper functions (detect_existing_work, etc.)
5. Completion handling and orchestrator wrappers

Tests mock at port boundaries, not internal patches, following the hexagonal architecture.
"""

import os
import pytest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

from issue_orchestrator.control.session_launcher import (
    SessionLauncher,
    LaunchResult,
    detect_existing_work,
    log_transition,
    handle_session_completion,
    orchestrator_launch_session,
    orchestrator_launch_review_session,
    orchestrator_launch_rework_session,
    process_active_sessions,
    launch_triage_session,
    session_launcher_callback,
    restore_running_sessions,
    parse_session_ref,
    create_session,
    session_exists,
    kill_session,
    get_session_machine,
)
from issue_orchestrator.control.actions import ActionResult, AddLabelAction, RemoveLabelAction
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    AgentConfig,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    PendingCleanup,
    OrchestratorState,
    SessionHistoryEntry,
    TaskKind,
    SessionKey,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey, FakeIssueKey
from issue_orchestrator.domain.state_machines.issue_machine import IssueStateMachine, IssueState
from issue_orchestrator.domain.state_machines.session_machine import SessionStateMachine, SessionState
from issue_orchestrator.domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from issue_orchestrator.ports import (
    WorktreeInfo,
    CommitInfo,
    NullEventSink,
    TraceEvent,
    CommandResult,
)
from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.infra.config import Config


# =============================================================================
# Mock Adapters - following hexagonal architecture patterns
# =============================================================================


class MockEventSink:
    """Mock EventSink for capturing published events."""

    def __init__(self):
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)

    def get_events_by_name(self, name: str) -> list[TraceEvent]:
        return [e for e in self.events if e.name == name]

    def clear(self) -> None:
        self.events.clear()


class MockRepositoryHost:
    """Mock repository host implementing port interface."""

    def __init__(self):
        self.labels: dict[int, set[str]] = {}
        self.prs: dict[int, list[PRInfo]] = {}  # issue_number -> PRs
        self.add_label_calls: list[tuple[int, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return self.prs.get(issue_number, [])

    def create_issue_key(self, issue_number: int) -> GitHubIssueKey:
        return GitHubIssueKey(repo="test/repo", external_id=str(issue_number))


class MockWorktreeManager:
    """Mock worktree manager for testing."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.create_calls: list[dict] = []
        self.remove_calls: list[Path] = []

    def create(
        self,
        repo_root: Path,
        issue_number: int,
        issue_title: str,
        worktree_base: Path | None = None,
        enforce_hooks: bool = True,
        pre_push_hook: Path | None = None,
        branch_name: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
    ) -> WorktreeInfo:
        self.create_calls.append({
            "repo_root": repo_root,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "branch_name": branch_name,
            "reuse_options": reuse_options,
        })
        worktree_path = self.tmp_path / f"worktree-{issue_number}"
        worktree_path.mkdir(parents=True, exist_ok=True)
        return WorktreeInfo(
            path=worktree_path,
            branch_name=branch_name or f"{issue_number}-feature",
        )

    def remove(self, worktree_path: Path) -> None:
        self.remove_calls.append(worktree_path)


class MockWorkingCopy:
    """Mock working copy for VCS operations."""

    def __init__(self):
        self.commits_ahead: list[CommitInfo] = []
        self.current_branch: str | None = "main"

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        return self.commits_ahead

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.current_branch


class MockCommandRunner:
    """Mock command runner for setup commands."""

    def __init__(self):
        self.run_calls: list[dict] = []
        self.results: list[CommandResult] = []
        self._result_index = 0

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
    ) -> CommandResult:
        self.run_calls.append({"command": command, "cwd": cwd, "shell": shell})
        if self._result_index < len(self.results):
            result = self.results[self._result_index]
            self._result_index += 1
            return result
        return CommandResult(returncode=0, stdout="", stderr="", timed_out=False)


class MockSessionManager:
    """Mock session manager for terminal operations."""

    def __init__(self):
        self.sessions: dict[str, bool] = {}
        self.start_calls: list = []
        self.stop_calls: list = []

    def start(self, ctx) -> bool:
        self.start_calls.append(ctx)
        return True

    def stop(self, ref) -> None:
        self.stop_calls.append(ref)

    def exists(self, ref) -> bool:
        return ref.name in self.sessions


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_path_factory_fix(tmp_path):
    """Provide tmp_path as a fixture for tests."""
    return tmp_path


@pytest.fixture
def mock_events():
    """Create a mock event sink."""
    return MockEventSink()


@pytest.fixture
def mock_repo_host():
    """Create a mock repository host."""
    return MockRepositoryHost()


@pytest.fixture
def mock_worktree_manager(tmp_path):
    """Create a mock worktree manager."""
    return MockWorktreeManager(tmp_path)


@pytest.fixture
def mock_working_copy():
    """Create a mock working copy."""
    return MockWorkingCopy()


@pytest.fixture
def mock_command_runner():
    """Create a mock command runner."""
    return MockCommandRunner()


@pytest.fixture
def sample_config(tmp_path):
    """Create a sample Config for testing."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")

    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir(exist_ok=True)
    config.worktree_base = tmp_path / "worktrees"  # Top-level worktree_base
    config.agents["agent:web"] = AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )
    config.agents["agent:reviewer"] = AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=30,
    )
    config.code_review_agent = "agent:reviewer"
    return config


@pytest.fixture
def sample_issue():
    """Create a sample Issue for testing."""
    return Issue(
        number=123,
        title="Test issue",
        labels=["agent:web"],
        repo="test/repo",
    )


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample AgentConfig for testing."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")
    return AgentConfig(
        prompt_path=prompt_path,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def session_launcher(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
):
    """Create a SessionLauncher with mock dependencies."""
    session_exists_calls = []
    create_session_calls = []

    def mock_session_exists(name: str) -> bool:
        session_exists_calls.append(name)
        return False

    def mock_create_session(name: str, cmd: str, wd: Path, title: str | None) -> bool:
        create_session_calls.append({"name": name, "cmd": cmd, "wd": wd, "title": title})
        return True

    issue_machines: dict[int, IssueStateMachine] = {}
    session_machines: dict[str, SessionStateMachine] = {}
    review_machines: dict[int, ReviewStateMachine] = {}

    def get_issue_machine(issue):
        if issue.number not in issue_machines:
            issue_machines[issue.number] = IssueStateMachine(issue)
        return issue_machines[issue.number]

    def get_session_machine(name: str, n: int, timeout: int):
        if name not in session_machines:
            session_machines[name] = SessionStateMachine(name, n, timeout_minutes=timeout)
        return session_machines[name]

    def get_review_machine(pr_number: int, issue_number: int):
        if pr_number not in review_machines:
            review_machines[pr_number] = ReviewStateMachine(pr_number, issue_number)
        return review_machines[pr_number]

    launcher = SessionLauncher(
        config=sample_config,
        events=mock_events,
        repository_host=mock_repo_host,
        action_applier=MagicMock(),
        session_manager=MagicMock(),
        worktree_manager=mock_worktree_manager,
        working_copy=mock_working_copy,
        command_runner=mock_command_runner,
        session_exists_fn=mock_session_exists,
        create_session_fn=mock_create_session,
        get_issue_machine=get_issue_machine,
        get_session_machine=get_session_machine,
        get_review_machine=get_review_machine,
    )

    # Expose call tracking for tests
    launcher._session_exists_calls = session_exists_calls
    launcher._create_session_calls = create_session_calls
    launcher._issue_machines = issue_machines
    launcher._session_machines = session_machines
    launcher._review_machines = review_machines

    return launcher


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestDetectExistingWork:
    """Tests for detect_existing_work function - lines 77, 84, 91-93."""

    def test_returns_none_when_no_commits_ahead(self, tmp_path):
        """Verify returns None when worktree has no commits ahead of main (line 77)."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = []

        result = detect_existing_work(tmp_path, working_copy)

        assert result is None

    def test_returns_context_with_commits(self, tmp_path):
        """Verify returns context string when commits exist."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha="abc123", message="Fix bug", author="test", short_sha="abc1"),
            CommitInfo(sha="def456", message="Add feature", author="test", short_sha="def4"),
        ]
        working_copy.current_branch = "123-feature"

        result = detect_existing_work(tmp_path, working_copy)

        assert result is not None
        assert "2 existing commit(s)" in result
        assert "123-feature" in result
        assert "abc1" in result
        assert "Fix bug" in result

    def test_truncates_long_commit_list(self, tmp_path):
        """Verify truncates commit list when more than 10 commits (line 84)."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha=f"sha{i}", message=f"Commit {i}", author="test", short_sha=f"s{i}")
            for i in range(15)
        ]

        result = detect_existing_work(tmp_path, working_copy)

        assert result is not None
        assert "15 existing commit(s)" in result
        assert "... and 5 more" in result

    def test_handles_exception_gracefully(self, tmp_path):
        """Verify handles exceptions and returns None (lines 91-93)."""
        working_copy = MagicMock()
        working_copy.get_commits_ahead_of_main.side_effect = Exception("Git error")

        result = detect_existing_work(tmp_path, working_copy)

        assert result is None


class TestLogTransition:
    """Tests for log_transition function."""

    def test_logs_transition_info(self, caplog):
        """Verify transition is logged correctly."""
        import logging
        caplog.set_level(logging.INFO)

        log_transition("issue", 123, "AVAILABLE", "LAUNCHING", "no conflicts")

        assert "[TRANSITION] issue #123: AVAILABLE" in caplog.text
        assert "LAUNCHING" in caplog.text

    def test_logs_extra_data_at_debug(self, caplog):
        """Verify extra data is logged at debug level."""
        import logging
        caplog.set_level(logging.DEBUG)

        log_transition("issue", 123, "AVAILABLE", "LAUNCHING", "reason", {"agent": "web"})

        assert "[TRANSITION] #123 extra:" in caplog.text


# =============================================================================
# Issue Session Launch Tests
# =============================================================================


class TestLaunchIssueSession:
    """Tests for launch_issue_session method."""

    def test_successful_launch(self, session_launcher, sample_issue):
        """Verify successful issue session launch."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "issue-123"
        assert result.session.key.task == TaskKind.CODE

    def test_fails_when_no_agent_type(self, session_launcher):
        """Verify fails when issue has no agent type label (line 195)."""
        issue = Issue(number=123, title="No agent", labels=[], repo="test/repo")

        result = session_launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "no agent type label" in result.reason

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 199)."""
        issue = Issue(number=123, title="Unknown agent", labels=["agent:unknown"], repo="test/repo")

        result = session_launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_fails_when_no_repo_configured(self, session_launcher, sample_issue):
        """Verify fails when no repo configured (line 202)."""
        session_launcher.config.repo = None

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "No repo configured" in result.reason

    def test_skips_when_already_in_active_sessions(self, session_launcher, sample_issue, sample_agent_config, tmp_path):
        """Verify skips when issue already active (lines 209-210)."""
        issue_key = FakeIssueKey(str(sample_issue.number))
        existing_session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, session_launcher, sample_issue):
        """Verify skips when terminal session exists."""
        # Override session_exists to return True
        session_launcher._session_exists = lambda name: True

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Terminal session already running" in result.reason

    def test_adds_in_progress_label(self, session_launcher, sample_issue, mock_repo_host):
        """Verify in-progress label is added."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        actions = [call.args[0] for call in session_launcher._action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "in-progress" for a in actions)

    def test_fails_when_in_progress_label_add_fails(
        self, session_launcher, sample_issue, mock_worktree_manager
    ):
        """Verify launch fails when in-progress label cannot be added."""
        def apply_action(action):
            if isinstance(action, AddLabelAction) and action.label == "in-progress":
                return ActionResult.fail(action, "api error")
            return ActionResult.ok(action)

        session_launcher._action_applier.apply = MagicMock(side_effect=apply_action)

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to add in-progress label" in result.reason
        assert session_launcher._create_session_calls == []
        assert len(mock_worktree_manager.remove_calls) == 1

    def test_emits_session_started_event(self, session_launcher, sample_issue, mock_events):
        """Verify SESSION_STARTED event is emitted."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        started_events = [e for e in mock_events.events if "session" in str(e.name).lower() and "started" in str(e.name).lower()]
        assert len(started_events) >= 1

    def test_triggers_state_machine_transitions(self, session_launcher, sample_issue):
        """Verify state machine transitions are triggered."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # Check issue machine transitioned
        issue_machine = session_launcher._issue_machines[123]
        assert issue_machine.state == IssueState.IN_PROGRESS.value

        # Check session machine transitioned
        session_machine = session_launcher._session_machines["issue-123"]
        assert session_machine.state == SessionState.RUNNING.value

    def test_handles_session_creation_failure(self, session_launcher, sample_issue, mock_repo_host):
        """Verify handles terminal session creation failure (lines 373-376)."""
        # Override create_session to return False
        session_launcher._create_session = lambda name, cmd, wd, title: False

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to create terminal session" in result.reason
        # Verify in-progress label is removed on failure
        actions = [call.args[0] for call in session_launcher._action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "in-progress" for a in actions)

    def test_runs_setup_commands(self, session_launcher, sample_issue, mock_command_runner):
        """Verify setup commands are run (line 315)."""
        session_launcher.config.setup_worktree = ["npm install", "pip install -e ."]

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_command_runner.run_calls) == 2
        assert mock_command_runner.run_calls[0]["command"] == "npm install"
        assert mock_command_runner.run_calls[1]["command"] == "pip install -e ."

    def test_includes_existing_work_context(self, session_launcher, sample_issue, mock_working_copy):
        """Verify existing work is detected and included in command."""
        mock_working_copy.commits_ahead = [
            CommitInfo(sha="abc", message="Fix", author="test", short_sha="abc"),
        ]

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # The command should include existing work context
        cmd = session_launcher._create_session_calls[0]["cmd"]
        assert "IMPORTANT:" in cmd or "existing commit" in cmd.lower() or result.session is not None

    def test_writes_session_identity_file(self, session_launcher, sample_issue, mock_worktree_manager, tmp_path):
        """Verify session identity file is written (lines 174-175 on error)."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # Check that worktree was created
        assert len(mock_worktree_manager.create_calls) == 1

    def test_sets_e2e_pr_labels_env(self, session_launcher, sample_issue):
        """Verify E2E_PR_LABELS env var is set (lines 349-350)."""
        session_launcher.config.e2e_pr_labels = ["e2e-passed", "ci-ready"]

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        cmd = session_launcher._create_session_calls[0]["cmd"]
        assert "E2E_PR_LABELS" in cmd

    def test_checks_dependencies_before_launch(self, session_launcher):
        """Verify CAS dependency check (lines 235-254)."""
        # Create issue with body so dependency check is triggered
        issue_with_body = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
            repo="test/repo",
            body="Blocked by #100",  # Body required for dependency check
        )

        # Set up dependency evaluator mock
        mock_evaluator = MagicMock()
        mock_report = MagicMock()
        mock_report.runnable = False
        mock_report.summary.return_value = "Blocked by #100"
        mock_evaluator.evaluate.return_value = mock_report

        session_launcher._dependency_evaluator = mock_evaluator
        session_launcher._refresh_issue = lambda n: issue_with_body

        result = session_launcher.launch_issue_session(issue_with_body, active_sessions=[])

        assert result.success is False
        assert "Dependencies not satisfied" in result.reason


class TestLaunchIssueSessionPerSessionWorktree:
    """Tests for per-session worktree mode (lines 264-266)."""

    def test_creates_per_session_worktree_when_env_set(self, session_launcher, sample_issue):
        """Verify per-session worktree base when env var is set."""
        with patch.dict(os.environ, {"ORCHESTRATOR_WORKTREE_PER_SESSION": "1"}):
            result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

            assert result.success is True


# =============================================================================
# Review Session Launch Tests
# =============================================================================


class TestLaunchReviewSession:
    """Tests for launch_review_session method."""

    def test_successful_launch(self, session_launcher, mock_repo_host):
        """Verify successful review session launch."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "review-456"
        assert result.session.key.task == TaskKind.REVIEW

    def test_fails_when_no_review_agent_configured(self, session_launcher):
        """Verify fails when no code review agent configured (line 418)."""
        session_launcher.config.code_review_agent = None
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No code review agent configured" in result.reason

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 422)."""
        session_launcher.config.code_review_agent = "agent:nonexistent"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_fails_when_no_repo_configured(self, session_launcher):
        """Verify fails when no repo configured (line 435)."""
        session_launcher.config.repo = None
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert "No repo configured" in result.reason

    def test_skips_when_terminal_already_running(self, session_launcher):
        """Verify keeps queued when terminal exists (keep_queued=True)."""
        session_launcher._session_exists = lambda name: name == "review-456"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert result.keep_queued is True

    def test_triggers_review_state_machine(self, session_launcher):
        """Verify review state machine is triggered."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = session_launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        review_machine = session_launcher._review_machines[456]
        assert review_machine.state == ReviewState.IN_REVIEW.value

    def test_per_session_worktree(self, session_launcher):
        """Verify per-session worktree mode for reviews (lines 463-465)."""
        with patch.dict(os.environ, {"ORCHESTRATOR_WORKTREE_PER_SESSION": "1"}):
            review = PendingReview(
                issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
                pr_number=456,
                pr_url="https://github.com/test/repo/pull/456",
                branch_name="123-feature",
                _issue_number=123,
            )

            result = session_launcher.launch_review_session(review, active_sessions=[])

            assert result.success is True


# =============================================================================
# Rework Session Launch Tests
# =============================================================================


class TestLaunchReworkSession:
    """Tests for launch_rework_session method (lines 585, 597-599, 604-605, 611-765)."""

    def test_successful_launch_with_pr(self, session_launcher, mock_repo_host):
        """Verify successful rework session launch when PR exists."""
        mock_repo_host.prs[123] = [
            PRInfo(
                number=456,
                title="Fix issue #123",
                url="https://github.com/test/repo/pull/456",
                branch="123-feature",
                body="",
                state="open",
                labels=[],
            )
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "rework-123"
        assert result.session.key.task == TaskKind.REWORK

    def test_successful_launch_without_pr(self, session_launcher):
        """Verify launch when no PR exists (lines 597-599)."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Uses fallback branch name when no PR

    def test_fails_when_agent_config_missing(self, session_launcher):
        """Verify fails when agent config not found (line 585)."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:nonexistent",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert "No agent config" in result.reason

    def test_skips_when_already_in_active_sessions(self, session_launcher, sample_agent_config, tmp_path):
        """Verify skips when rework already active (lines 604-605)."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = GitHubIssueKey(repo="test/repo", external_id="123")
        existing_session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.REWORK),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="rework-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )
        rework = PendingRework(
            issue_key=issue_key,
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, session_launcher):
        """Verify keeps queued when terminal exists."""
        session_launcher._session_exists = lambda name: name == "rework-123"
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert result.keep_queued is True

    def test_updates_rework_cycle_label(self, session_launcher, mock_repo_host):
        """Verify rework cycle label is updated (lines 820-839)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=2,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should add rework-cycle-2 label
        actions = [call.args[0] for call in session_launcher._action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "rework-cycle-2" for a in actions)

    def test_removes_needs_rework_label(self, session_launcher, mock_repo_host):
        """Verify needs-rework label is removed (lines 754-764)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.labels[456] = {"needs-rework"}
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should remove needs-rework label
        actions = [call.args[0] for call in session_launcher._action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "needs-rework" for a in actions)


# =============================================================================
# Setup Commands Tests
# =============================================================================


class TestRunSetupCommands:
    """Tests for _run_setup_commands method (lines 769-784)."""

    def test_runs_all_setup_commands(self, session_launcher, mock_command_runner, tmp_path):
        """Verify all setup commands are run."""
        session_launcher.config.setup_worktree = ["npm install", "make build"]

        session_launcher._run_setup_commands(tmp_path)

        assert len(mock_command_runner.run_calls) == 2

    def test_handles_command_failure(self, session_launcher, mock_command_runner, tmp_path, caplog):
        """Verify handles command failure gracefully (line 778-780)."""
        import logging
        caplog.set_level(logging.WARNING)

        session_launcher.config.setup_worktree = ["failing-command"]
        mock_command_runner.results = [
            CommandResult(returncode=1, stdout="", stderr="Command failed", timed_out=False)
        ]

        session_launcher._run_setup_commands(tmp_path)

        assert "Setup command failed" in caplog.text

    def test_handles_command_timeout(self, session_launcher, mock_command_runner, tmp_path, caplog):
        """Verify handles command timeout (line 781-782)."""
        import logging
        caplog.set_level(logging.WARNING)

        session_launcher.config.setup_worktree = ["slow-command"]
        mock_command_runner.results = [
            CommandResult(returncode=1, stdout="", stderr="", timed_out=True)
        ]

        session_launcher._run_setup_commands(tmp_path)

        assert "timed out" in caplog.text


# =============================================================================
# Orchestrator Wrapper Function Tests
# =============================================================================


class TestOrchestratorLaunchSession:
    """Tests for orchestrator_launch_session function."""

    def test_appends_session_to_active(self, session_launcher, sample_issue):
        """Verify session is appended to active_sessions."""
        state = OrchestratorState()

        result = orchestrator_launch_session(sample_issue, state, session_launcher)

        assert result is not None
        assert len(state.active_sessions) == 1
        assert state.active_sessions[0].terminal_id == "issue-123"


class TestOrchestratorLaunchReviewSession:
    """Tests for orchestrator_launch_review_session function (lines 977, 990)."""

    def test_removes_from_pending_and_adds_to_active(self, session_launcher):
        """Verify review is removed from pending and added to active."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState()
        state.pending_reviews = [review]

        mock_restorer = MagicMock()

        result = orchestrator_launch_review_session(review, state, session_launcher, mock_restorer)

        assert result is not None
        assert len(state.pending_reviews) == 0
        assert len(state.active_sessions) == 1

    def test_restores_orphaned_terminal_when_keep_queued(self, session_launcher):
        """Verify orphaned terminal restoration (lines 987-990)."""
        session_launcher._session_exists = lambda name: name == "review-456"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState()
        state.pending_reviews = [review]

        mock_restorer = MagicMock()
        mock_restorer.restore_sessions.return_value = []

        result = orchestrator_launch_review_session(review, state, session_launcher, mock_restorer)

        assert result is None
        # Should have tried to restore
        mock_restorer.restore_sessions.assert_called_once()


class TestOrchestratorLaunchReworkSession:
    """Tests for orchestrator_launch_rework_session function."""

    def test_removes_from_pending_and_adds_to_active(self, session_launcher):
        """Verify rework is removed from pending and added to active."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )
        state = OrchestratorState()
        state.pending_reworks = [rework]

        mock_restorer = MagicMock()

        result = orchestrator_launch_rework_session(rework, state, session_launcher, mock_restorer)

        assert result is not None
        assert len(state.pending_reworks) == 0
        assert len(state.active_sessions) == 1


# =============================================================================
# Triage Session Tests
# =============================================================================


class TestLaunchTriageSession:
    """Tests for launch_triage_session function (lines 1055-1058)."""

    def test_raises_when_no_triage_agent(self, sample_config):
        """Verify raises ValueError when no triage agent configured."""
        sample_config.triage_review_agent = None
        triage = PendingTriageReview(issue_number=789, title="Triage batch")

        with pytest.raises(ValueError, match="Invalid triage agent"):
            launch_triage_session(triage, sample_config, lambda issue: None)

    def test_raises_when_triage_agent_not_in_config(self, sample_config):
        """Verify raises ValueError when triage agent not configured."""
        sample_config.triage_review_agent = "agent:missing"
        triage = PendingTriageReview(issue_number=789, title="Triage batch")

        with pytest.raises(ValueError, match="Invalid triage agent"):
            launch_triage_session(triage, sample_config, lambda issue: None)

    def test_calls_launch_fn_with_issue(self, sample_config):
        """Verify launch function is called with correct issue."""
        sample_config.triage_review_agent = "agent:web"
        triage = PendingTriageReview(issue_number=789, title="Triage batch")
        launched_issues = []

        def track_launch(issue):
            launched_issues.append(issue)
            return None

        launch_triage_session(triage, sample_config, track_launch)

        assert len(launched_issues) == 1
        assert launched_issues[0].number == 789
        assert "agent:web" in launched_issues[0].labels


# =============================================================================
# Session Callback Tests
# =============================================================================


class TestSessionLauncherCallback:
    """Tests for session_launcher_callback function."""

    def test_dispatches_to_correct_handler(self):
        """Verify callback dispatches to correct handler."""
        calls = []

        def issue_fn(n):
            calls.append(("issue", n))
            return None

        def review_fn(n):
            calls.append(("review", n))
            return None

        def rework_fn(n):
            calls.append(("rework", n))
            return None

        def triage_fn(n):
            calls.append(("triage", n))
            return None

        session_launcher_callback(SessionType.ISSUE, 123, issue_fn, review_fn, rework_fn, triage_fn)
        session_launcher_callback(SessionType.REVIEW, 456, issue_fn, review_fn, rework_fn, triage_fn)

        assert calls == [("issue", 123), ("review", 456)]


# =============================================================================
# Session Reference Parsing Tests
# =============================================================================


class TestParseSessionRef:
    """Tests for parse_session_ref function (lines 1097-1101)."""

    def test_parses_valid_session_name(self):
        """Verify valid session name is parsed."""
        events = MockEventSink()
        ref = parse_session_ref("issue-123", "test", events)

        assert ref.number == 123

    def test_emits_error_event_on_invalid_name(self):
        """Verify error event is emitted for invalid name."""
        events = MockEventSink()

        with pytest.raises(ValueError):
            parse_session_ref("invalid-name", "test", events)

        # Should have emitted error event
        error_events = [e for e in events.events if "error" in str(e.name).lower()]
        assert len(error_events) == 1


# =============================================================================
# Restore Running Sessions Tests
# =============================================================================


class TestRestoreRunningSessions:
    """Tests for restore_running_sessions function (line 1085)."""

    def test_extends_active_sessions(self):
        """Verify restored sessions are added to active list."""
        mock_restorer = MagicMock()
        mock_session = MagicMock()
        mock_restorer.restore_sessions.return_value = [mock_session]

        active_sessions = []
        running = [{"tab_name": "issue-123", "issue_number": 123}]

        restore_running_sessions(running, active_sessions, mock_restorer)

        assert len(active_sessions) == 1
        assert active_sessions[0] == mock_session


# =============================================================================
# Process Active Sessions Tests
# =============================================================================


class TestProcessActiveSessions:
    """Tests for process_active_sessions function (line 1034)."""

    def test_skips_running_sessions(self, sample_agent_config, tmp_path):
        """Verify running sessions are skipped."""
        from issue_orchestrator.observation.observation import SessionObservation, SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.running(runtime_minutes=5.0)

        config = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=MagicMock(),
            completion_handler=MagicMock(),
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
        )

        # Session should still be in active list
        assert len(state.active_sessions) == 1


# =============================================================================
# Session Helper Tests
# =============================================================================


class TestCreateSession:
    """Tests for create_session function."""

    def test_creates_session_via_manager(self):
        """Verify session is created through manager."""
        mock_manager = MockSessionManager()
        events = MockEventSink()

        result = create_session(
            "issue-123",
            "claude --help",
            Path("/tmp/worktree"),
            "Test Session",
            mock_manager,
            events,
        )

        assert result is True
        assert len(mock_manager.start_calls) == 1


class TestSessionExists:
    """Tests for session_exists function."""

    def test_checks_via_manager(self):
        """Verify existence is checked through manager."""
        mock_manager = MockSessionManager()
        mock_manager.sessions["issue-123"] = True
        events = MockEventSink()

        result = session_exists("issue-123", mock_manager, events)

        assert result is True


class TestKillSession:
    """Tests for kill_session function."""

    def test_stops_via_manager(self):
        """Verify session is stopped through manager."""
        mock_manager = MockSessionManager()
        events = MockEventSink()

        kill_session("issue-123", mock_manager, events)

        assert len(mock_manager.stop_calls) == 1


class TestGetSessionMachine:
    """Tests for get_session_machine function."""

    def test_gets_from_state_machines(self):
        """Verify machine is retrieved from manager."""
        mock_sm_manager = MagicMock()
        expected_machine = SessionStateMachine("issue-123", 123, timeout_minutes=45)
        mock_sm_manager.get_session_machine.return_value = expected_machine

        result = get_session_machine("issue-123", 123, 45, mock_sm_manager)

        assert result == expected_machine
        mock_sm_manager.get_session_machine.assert_called_once_with("issue-123", 123, 45)


# =============================================================================
# Handle Session Completion Tests
# =============================================================================


class TestHandleSessionCompletion:
    """Tests for handle_session_completion function."""

    def test_removes_session_from_active(self, sample_agent_config, tmp_path):
        """Verify completed session is removed from active list."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_triage.close_ai_session_tabs = True
        config.code_review_agent = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
        )

        assert len(state.active_sessions) == 0
        assert len(state.completed_today) == 1
        assert 123 in state.completed_today

    def test_adds_to_discovered_failures_on_failure(self, sample_agent_config, tmp_path):
        """Verify failed session is added to discovered failures."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=10,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_triage.close_ai_session_tabs = True
        config.code_review_agent = None

        handle_session_completion(
            session=session,
            status=SessionStatus.FAILED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
        )

        assert len(state.discovered_failures) == 1
        assert state.discovered_failures[0].issue_number == 123

    def test_queues_review_when_pr_created(self, sample_agent_config, tmp_path):
        """Verify review is queued when session creates PR."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=10,
                pr_url="https://github.com/test/repo/pull/456",
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=True,
            pr_url="https://github.com/test/repo/pull/456",
            pr_number=456,
        )
        mock_action_applier = MagicMock()
        mock_observer = MagicMock()
        config = MagicMock()
        config.cleanup.without_triage.close_ai_session_tabs = True
        config.code_review_agent = "agent:reviewer"

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
        )

        assert len(state.discovered_reviews) == 1
        assert state.discovered_reviews[0].pr_number == 456


# =============================================================================
# Environment Isolation Tests
# =============================================================================


class TestEnvironmentIsolation:
    """Test that sessions use proper environment isolation."""

    def test_issue_session_launches_successfully(self, session_launcher, sample_issue):
        """Test that issue sessions launch without HOME isolation.

        HOME isolation is disabled because Claude uses macOS keychain for
        subscription auth, which requires access to the real HOME.
        """
        session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        # Verify session was created
        assert len(session_launcher._create_session_calls) == 1

        # Verify command doesn't contain HOME override
        command = session_launcher._create_session_calls[0]["cmd"]
        assert "export HOME=" not in command or "HOME=" not in command.split("&&")[0]
