"""Unit tests for SessionLauncher - behavior-centric tests.

These tests verify:
1. Issue session launching (happy path, error cases, state transitions)
2. Review session launching (happy path, conflicts, state transitions)
3. Rework session launching (happy path, PR resolution, conflicts)
4. Helper functions (detect_existing_work, etc.)
5. Completion handling and orchestrator wrappers

Tests mock at port boundaries, not internal patches, following the hexagonal architecture.
"""

import json
import os
import shlex
import pytest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, cast
from unittest.mock import MagicMock, patch

from issue_orchestrator.control.session_completion import (
    _apply_completed_decisions,
    _record_provider_resilience_effects,
    _terminate_finished_session,
    handle_session_completion,
    process_active_sessions,
)
from issue_orchestrator.control.completion_dispatcher import CompletedDecision
from issue_orchestrator.control.session_decision import (
    ProviderTransientFailureDecision,
    SessionDecision,
)
from issue_orchestrator.control.session_launch_types import LaunchResult
from issue_orchestrator.control.session_launcher import (
    SessionLauncher,
    detect_existing_work,
    log_transition,
)
from issue_orchestrator.control.isolation import GRADLE_USER_HOME_ENV
from issue_orchestrator.control.session_review_support import build_review_existing_work
from issue_orchestrator.control.session_routing import (
    orchestrator_launch_session,
    orchestrator_launch_review_session,
    orchestrator_launch_rework_session,
    orchestrator_launch_validation_retry_session,
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
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.retrospective_review_completion import (
    retrospective_review_completion_actions,
)
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.domain.models import (
    Issue,
    ORCHESTRATOR_PR_MARKER,
    Session,
    SessionStatus,
    AgentConfig,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
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
from issue_orchestrator.events import EventName
from issue_orchestrator.ports import (
    WorktreeInfo,
    CommitInfo,
    NullEventSink,
    TraceEvent,
    CommandResult,
    NullManifestDownloader,
)
from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions
from issue_orchestrator.ports.pull_request_tracker import PRInfo, PRRef
from issue_orchestrator.ports.session_output import SessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.contracts.public import SessionStartedPayload
from tests.unit.session_run_helpers import make_session_run_assets


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
        self.issues: dict[int, Issue] = {}
        self.prs: dict[int, list[PRInfo]] = {}  # issue_number -> PRs
        self.pr_reviews: dict[int, list[dict]] = {}  # pr_number -> reviews
        self.add_label_calls: list[tuple[int, str]] = []
        self.remove_label_calls: list[tuple[int, str]] = []
        self.search_pr_refs_calls: list[int] = []

    def add_label(self, issue_number: int, label: str) -> None:
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return self.prs.get(issue_number, [])

    def search_pr_refs_for_issue(self, issue_number: int) -> list[PRRef]:
        self.search_pr_refs_calls.append(issue_number)
        return [
            PRRef(number=pr.number, url=pr.url, title=pr.title, body=pr.body)
            for pr in self.prs.get(issue_number, [])
        ]

    def get_pr(self, pr_number: int) -> PRInfo | None:
        for prs in self.prs.values():
            for pr in prs:
                if pr.number == pr_number:
                    return pr
        return None

    def get_issue(self, issue_number: int) -> Issue | None:
        issue = self.issues.get(issue_number)
        if issue is not None:
            return issue
        labels = sorted(self.labels.get(issue_number, set()))
        if not any(label.startswith("agent:") for label in labels):
            labels.append("agent:web")
        return Issue(
            number=issue_number,
            title=f"Issue {issue_number}",
            labels=labels,
            repo="test/repo",
        )

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        return self.pr_reviews.get(pr_number, [])

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
        base_branch: str | None = None,
        seed_ref: str | None = None,
        reuse_options: WorktreeReuseOptions | None = None,
    ) -> WorktreeInfo:
        self.create_calls.append({
            "repo_root": repo_root,
            "issue_number": issue_number,
            "issue_title": issue_title,
            "enforce_hooks": enforce_hooks,
            "base_branch": base_branch,
            "seed_ref": seed_ref,
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
        self.head_sha: str | None = None

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        return self.commits_ahead

    def get_current_branch(self, worktree: Path) -> str | None:
        return self.current_branch

    def get_head_sha(self, worktree: Path) -> str | None:
        return self.head_sha


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
        self.run_calls.append({"command": command, "cwd": cwd, "env": env, "shell": shell})
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
    config.setup_worktree = []
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


@dataclass
class LauncherTestBundle:
    """Bundle of launcher and tracking objects for tests."""
    launcher: SessionLauncher
    session_exists_calls: list
    create_session_calls: list
    issue_machines: dict
    session_machines: dict
    review_machines: dict
    # Stores [callable] so tests can override behavior
    session_exists_override: list
    create_session_override: list = field(default_factory=lambda: [None])
    # Injected mocks for test assertions
    action_applier: MagicMock = field(default_factory=MagicMock)


@pytest.fixture
def launcher_bundle(
    sample_config,
    mock_events,
    mock_repo_host,
    mock_worktree_manager,
    mock_working_copy,
    mock_command_runner,
) -> LauncherTestBundle:
    """Create a SessionLauncher with mock dependencies and tracking.

    Returns a bundle with the launcher and tracking objects for test assertions.
    """
    session_exists_calls = []
    create_session_calls = []
    session_exists_override = [None]  # List so tests can replace the callable

    def mock_session_exists(name: str) -> bool:
        session_exists_calls.append(name)
        if session_exists_override[0] is not None:
            return session_exists_override[0](name)
        return False

    create_session_override = [None]  # List so tests can replace the callable

    def mock_create_session(name: str, cmd: str, wd: Path, title: str | None) -> bool:
        create_session_calls.append({"name": name, "cmd": cmd, "wd": wd, "title": title})
        if create_session_override[0] is not None:
            return create_session_override[0](name, cmd, wd, title)
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

    def remove_session_machine(name: str) -> None:
        session_machines.pop(name, None)

    def get_review_machine(pr_number: int, issue_number: int):
        if pr_number not in review_machines:
            review_machines[pr_number] = ReviewStateMachine(pr_number, issue_number)
        return review_machines[pr_number]

    mock_action_applier = MagicMock()
    launcher = SessionLauncher(
        config=sample_config,
        events=mock_events,
        repository_host=mock_repo_host,
        action_applier=mock_action_applier,
        session_manager=MagicMock(),
        worktree_manager=mock_worktree_manager,
        working_copy=mock_working_copy,
        command_runner=mock_command_runner,
        session_output=FileSystemSessionOutput(),
        manifest_downloader=NullManifestDownloader(),
        session_exists_fn=mock_session_exists,
        create_session_fn=mock_create_session,
        get_issue_machine=get_issue_machine,
        get_session_machine=get_session_machine,
        get_review_machine=get_review_machine,
        remove_session_machine=remove_session_machine,
    )

    bundle = LauncherTestBundle(
        launcher=launcher,
        session_exists_calls=session_exists_calls,
        create_session_calls=create_session_calls,
        issue_machines=issue_machines,
        session_machines=session_machines,
        review_machines=review_machines,
        session_exists_override=session_exists_override,
        create_session_override=create_session_override,
        action_applier=mock_action_applier,
    )
    return bundle


@pytest.fixture
def session_launcher(launcher_bundle: LauncherTestBundle) -> SessionLauncher:
    """Convenience fixture for tests that only need the launcher."""
    return launcher_bundle.launcher


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

    def test_returns_none_when_head_matches_seed_ref(self, tmp_path):
        """Seeded local issue worktrees should not surface inherited base commits as existing work."""
        working_copy = MockWorkingCopy()
        working_copy.commits_ahead = [
            CommitInfo(sha="abc123", message="Base fix", author="test", short_sha="abc1"),
        ]
        working_copy.head_sha = "abc123"

        result = detect_existing_work(tmp_path, working_copy, seed_ref="abc123")

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
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-1")

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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, launcher_bundle, sample_issue):
        """Verify skips when terminal session exists."""
        # Override session_exists to return True
        launcher_bundle.session_exists_override[0] = lambda name: True

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Terminal session already running" in result.reason
        assert result.keep_queued is True

    def test_adds_in_progress_label(self, launcher_bundle, sample_issue, mock_repo_host):
        """Verify in-progress label is added."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "in-progress" for a in actions)

    def test_issue_launch_clears_coding_interrupted_guard_label(self, launcher_bundle, sample_issue):
        """Issue launch should clear interrupted coding retry guard."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.coding_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == sample_issue.number
            and a.label == guard_label
            for a in actions
        )

    def test_issue_launch_from_scratch_forces_fresh_worktree_branch(
        self,
        launcher_bundle,
        sample_issue,
        mock_worktree_manager,
        mock_events,
    ):
        """Scratch pending label should force fresh worktree + fresh branch from base."""
        scratch_label = launcher_bundle.launcher._lm.reset_retry_scratch_pending  # noqa: SLF001
        sample_issue.labels = [*sample_issue.labels, scratch_label]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_worktree_manager.create_calls) == 1
        create_call = mock_worktree_manager.create_calls[0]
        reuse_options = create_call["reuse_options"]
        assert reuse_options is not None
        assert reuse_options.disable_reuse is True
        assert reuse_options.worktree_branch_on_recreate == "create_new_branch"
        branch_name = create_call["branch_name"]
        assert isinstance(branch_name, str)
        assert branch_name.startswith(f"{sample_issue.number}-scratch-")
        started = next(e for e in mock_events.events if str(e.name) == "session.started")
        assert started.data["reset_from_scratch"] is True
        run_dir = Path(started.data["run_dir"])
        manifest = json.loads((run_dir / "manifest.json").read_text())
        assert manifest["reset_from_scratch"] is True
        assert manifest["review_cache_boundary"] == "scratch_reset"
        assert manifest["review_cache_boundary_started_at"] == manifest["started_at"]

        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == sample_issue.number
            and a.label == scratch_label
            for a in actions
        )

    def test_fails_when_in_progress_label_add_fails(
        self, launcher_bundle, sample_issue, mock_worktree_manager
    ):
        """Verify launch fails when in-progress label cannot be added."""
        def apply_action(action):
            if isinstance(action, AddLabelAction) and action.label == "in-progress":
                return ActionResult.fail(action, "api error")
            return ActionResult.ok(action)

        launcher_bundle.action_applier.apply = MagicMock(side_effect=apply_action)

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to add in-progress label" in result.reason
        assert launcher_bundle.create_session_calls == []
        assert len(mock_worktree_manager.remove_calls) == 1

    def test_emits_session_started_event(self, session_launcher, sample_issue, mock_events):
        """Verify SESSION_STARTED event is emitted with expected payload."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        started_events = mock_events.get_events_by_name("session.started")
        assert len(started_events) == 1
        payload = started_events[0].data
        assert payload["issue_number"] == sample_issue.number
        assert {"session_id", "worktree_path", "branch_name", "completion_path", "completion_path_absolute"}.issubset(
            payload.keys()
        )
        worktree_path = Path(payload["worktree_path"])
        completion_path = payload["completion_path"]
        completion_parts = Path(completion_path).parts
        assert "sessions" in completion_parts
        assert any(part.endswith("__coding-1") for part in completion_parts)
        assert payload["completion_path_absolute"] == str((worktree_path / completion_path).resolve())
        SessionStartedPayload.model_validate(payload)

    def test_triggers_state_machine_transitions(self, launcher_bundle, sample_issue):
        """Verify state machine transitions are triggered."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # Check issue machine transitioned
        issue_machine = launcher_bundle.issue_machines[123]
        assert issue_machine.state == IssueState.IN_PROGRESS.value

        # Check session machine transitioned
        session_machine = launcher_bundle.session_machines["issue-123"]
        assert session_machine.state == SessionState.RUNNING.value

    def test_resets_non_pending_session_machine_on_launch(self, launcher_bundle, sample_issue):
        """Launch resets an unexpected session machine state before transitioning."""
        original = SessionStateMachine("issue-123", 123, timeout_minutes=30)
        original.launch()
        original.started()
        launcher_bundle.session_machines["issue-123"] = original

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        replacement = launcher_bundle.session_machines["issue-123"]
        assert replacement is not original
        assert replacement.state == SessionState.RUNNING.value

    def test_handles_session_creation_failure(self, launcher_bundle, sample_issue, mock_repo_host):
        """Verify handles terminal session creation failure (lines 373-376)."""
        # Override create_session to return False
        launcher_bundle.create_session_override[0] = lambda name, cmd, wd, title: False

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Failed to create terminal session" in result.reason
        # Verify in-progress label is removed on failure
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "in-progress" for a in actions)

    def test_runs_setup_commands(self, session_launcher, sample_issue, mock_command_runner):
        """Verify setup commands are run (line 315)."""
        session_launcher.config.setup_worktree = ["npm install", "pip install -e ."]

        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        assert len(mock_command_runner.run_calls) == 2
        assert mock_command_runner.run_calls[0]["command"] == "npm install"
        assert mock_command_runner.run_calls[1]["command"] == "pip install -e ."

    def test_fails_launch_when_setup_command_fails(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
        mock_worktree_manager,
    ):
        """Configured setup failures must stop launch before the agent starts."""
        launcher_bundle.launcher.config.setup_worktree = ["make worktree-setup"]
        mock_command_runner.results = [
            CommandResult(returncode=1, stdout="", stderr="missing node_modules", timed_out=False),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "Setup commands failed" in result.reason
        assert launcher_bundle.create_session_calls == []
        assert mock_worktree_manager.remove_calls, "failed setup should clean up the worktree"

    def test_fails_launch_when_setup_command_times_out(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
    ):
        """Timeouts should surface as timeouts even if the runner also reports a nonzero exit."""
        launcher_bundle.launcher.config.setup_worktree = ["make worktree-setup"]
        mock_command_runner.results = [
            CommandResult(returncode=137, stdout="", stderr="killed", timed_out=True),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is False
        assert "timed out" in result.reason
        assert "exit_code=137" not in result.reason

    def test_includes_existing_work_context(self, launcher_bundle, sample_issue, mock_working_copy):
        """Verify existing work is detected and included in command."""
        mock_working_copy.commits_ahead = [
            CommitInfo(sha="abc", message="Fix", author="test", short_sha="abc"),
        ]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # The command should include existing work context
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "IMPORTANT:" in cmd or "existing commit" in cmd.lower() or result.session is not None

    def test_writes_session_identity_file(self, session_launcher, sample_issue, mock_worktree_manager, tmp_path):
        """Verify session identity file is written (lines 174-175 on error)."""
        result = session_launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        # Check that worktree was created
        assert len(mock_worktree_manager.create_calls) == 1

    def test_sets_e2e_pr_labels_env(self, launcher_bundle, sample_issue):
        """Verify E2E_PR_LABELS env var is set (lines 349-350)."""
        launcher_bundle.launcher.config.e2e_pr_labels = ["e2e-passed", "ci-ready"]

        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "E2E_PR_LABELS" in cmd

    def test_exports_worktree_path_env_var(self, launcher_bundle, sample_issue):
        """Verify ISSUE_ORCHESTRATOR_WORKTREE is exported so coding-done can guard CWD."""
        result = launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert result.success is True
        cmd = launcher_bundle.create_session_calls[0]["cmd"]
        assert "ISSUE_ORCHESTRATOR_WORKTREE=" in cmd

    def test_checks_dependencies_before_launch(self, launcher_bundle):
        """Just-before-launch recheck consumes the work gate and blocks stale work."""
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator

        # Create issue with a real normal dependency so the work gate is consulted
        issue_with_body = Issue(
            number=123,
            title="Test issue",
            labels=["agent:web"],
            repo="test/repo",
            body="Depends-on: #100",
            milestone="M1",
        )

        class _Checker:
            def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
                return "open" if issue_number == 100 else None  # dependency still open

            def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
                return "M1"

        evaluator = DependencyEvaluator(issue_checker=_Checker(), events=NullEventSink())

        launcher_bundle.launcher._dependency_evaluator = evaluator  # noqa: SLF001
        launcher_bundle.launcher._refresh_issue = lambda n: issue_with_body  # noqa: SLF001

        result = launcher_bundle.launcher.launch_issue_session(issue_with_body, active_sessions=[])

        assert result.success is False
        assert "Dependencies not satisfied" in result.reason
        assert "#100" in result.reason

    def test_recheck_blocks_stale_stack_work_changed_after_planning(
        self, launcher_bundle, mock_events
    ):
        """Predecessor review state changing between planning and launch blocks start.

        The just-before-launch recheck re-gathers predecessor facts, so a stack
        successor that was work-ready at planning time must not start once the
        predecessor's branch/review state regresses (ADR-0029 race guard).
        """
        from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
        from issue_orchestrator.domain.dependency_gates import PredecessorFacts

        issue = Issue(
            number=300,
            title="Stacked successor",
            labels=["agent:web"],
            repo="test/repo",
            body="Stack-after: #200",
            milestone="M1",
        )

        class _Checker:
            def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
                return "open" if issue_number == 200 else None  # predecessor still open

            def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
                return "M1"

        class _MutableProvider:
            """Returns whatever facts are current at gather time."""

            def __init__(self):
                self.facts = PredecessorFacts(
                    branch_usable=True, validation_passed=True,
                    agent_reviewed=True, branch_name="200-base",
                )

            def gather_facts(self, targets):
                return {t: self.facts for t in targets}

        provider = _MutableProvider()
        evaluator = DependencyEvaluator(
            issue_checker=_Checker(), events=mock_events,
            predecessor_facts_provider=provider,
        )
        launcher_bundle.launcher._dependency_evaluator = evaluator  # noqa: SLF001
        launcher_bundle.launcher._refresh_issue = lambda n: issue  # noqa: SLF001

        # At planning time the predecessor was validated + reviewed -> work-ready.
        planning = evaluator.evaluate_work_gate(300, "Stack-after: #200", "M1", emit_event=False)
        assert planning.can_start_work is True

        # Predecessor review is withdrawn (e.g. a new push) before launch.
        provider.facts = PredecessorFacts(
            branch_usable=True, validation_passed=True, agent_reviewed=False,
        )
        mock_events.clear()

        result = launcher_bundle.launcher.launch_issue_session(issue, active_sessions=[])

        assert result.success is False
        assert "Dependencies not satisfied" in result.reason
        blocked = mock_events.get_events_by_name("issue.dependency_blocked")
        assert len(blocked) == 1
        data = blocked[0].data
        assert data["gate"] == "work"
        assert any(
            r["mode"] == "stack"
            and r["predecessor"] == "#200"
            and r["reason"] == "predecessor_review_pending"
            for r in data["blocked_reasons"]
        )


class TestLaunchValidationRetrySession:
    """Tests for validation retry launch behavior."""

    def test_launches_retry_with_retry_prompt_and_preserves_branch(
        self,
        launcher_bundle,
        mock_worktree_manager,
    ):
        """Validation retry launch uses the pending branch and retry prompt."""
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="Work on issue #123: Fix checkout",
            validation_error="Validation blocked before running command: dirty worktree",
            validation_error_file="/tmp/validation-errors.txt",
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )

        result = launcher_bundle.launcher.launch_validation_retry_session(
            retry,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "issue-123"
        assert result.session.validation_retry_count == 1
        assert result.session.original_prompt == "Work on issue #123: Fix checkout"
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-2")
        assert mock_worktree_manager.create_calls[0]["branch_name"] == "123-fix-checkout"
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "Validation Retry" in command
        assert "dirty worktree" in command


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
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__review-1")

    def test_review_launch_threads_issue_label_provider_args(self, launcher_bundle):
        """Label-derived provider args should reach review command and wrapper."""
        launcher_bundle.launcher.config.agents["agent:reviewer"].provider = "claude-code"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
            issue_labels=("agent:web", "verbose"),
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "--verbose" in shlex.split(command)

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

    def test_skips_when_terminal_already_running(self, launcher_bundle):
        """Verify keeps queued when terminal exists (keep_queued=True)."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert result.keep_queued is True

    def test_triggers_review_state_machine(self, launcher_bundle):
        """Verify review state machine is triggered."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True

    def test_review_launch_clears_review_interrupted_guard_label(self, launcher_bundle):
        """Review launch should clear interrupted review retry guard."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )

        result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.review_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == review.issue_number
            and a.label == guard_label
            for a in actions
        )
        review_machine = launcher_bundle.review_machines[456]
        assert review_machine.state == ReviewState.IN_REVIEW.value

    def test_drops_stale_pending_review_for_blocked_issue(
        self,
        launcher_bundle,
        mock_repo_host,
        caplog,
    ):
        """Blocked issues should invalidate queued review launches."""
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        mock_repo_host.issues[123] = Issue(
            number=123,
            title="Blocked issue",
            labels=["agent:web", "blocked-failed", "needs-rework"],
            repo="test/repo",
        )

        with caplog.at_level("INFO"):
            result = launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert result.success is False
        assert result.reason == "Stale pending review: issue_blocked"
        assert launcher_bundle.create_session_calls == []
        assert "Dropping stale pending review: pr=456 issue=123 reason=issue_blocked" in caplog.text

    def test_review_existing_work_includes_keep_current_note(
        self, launcher_bundle, mock_repo_host
    ):
        """Keep-current label should inject reviewer instruction."""
        keep_current_label = launcher_bundle.launcher.config.get_label_review_keep_current_approach()
        mock_repo_host.get_pr = MagicMock(return_value=PRInfo(
            number=456,
            title="Test PR",
            url="https://github.com/test/repo/pull/456",
            branch="issue-123",
            body="Test body",
            state="open",
            labels=[keep_current_label],
        ))

        worktree_info = WorktreeInfo(path=Path("/tmp/worktree"), branch_name="issue-123")

        note = build_review_existing_work(
            worktree_info=worktree_info,
            pr_number=456,
            repository_host=mock_repo_host,
            keep_current_label=keep_current_label,
        )

        assert note is not None
        assert "Keep the current approach" in note

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


class TestLaunchRetrospectiveReviewSession:
    """Tests for review-first existing-implementation sessions."""

    def test_successful_launch_records_review_first_identity(
        self,
        launcher_bundle,
        mock_events,
        mock_worktree_manager,
    ):
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session is not None
        assert result.session.terminal_id == "retrospective-review-365"
        assert result.session.key.task == TaskKind.RETROSPECTIVE_REVIEW
        assert result.session.pr_number == 512
        assert result.session.issue.labels == [
            "agent:web",
            "agent:reviewer",
            "lack-of-review-redo",
        ]
        assert "RETROSPECTIVE REVIEW MODE" in result.session.original_prompt
        assert "issue #365" in result.session.original_prompt
        assert "Prior orchestrator PR: #512" in result.session.original_prompt

        create_call = mock_worktree_manager.create_calls[0]
        assert create_call["issue_number"] == 365
        assert create_call["enforce_hooks"] is False
        reuse_options = create_call["reuse_options"]
        assert reuse_options is not None
        assert reuse_options.allow_remote_branch_delete is False

        event = next(e for e in mock_events.events if str(e.name) == str(EventName.REVIEW_STARTED))
        assert event.data["task"] == TaskKind.RETROSPECTIVE_REVIEW.value
        assert event.data["prior_pr_number"] == 512
        assert event.data["source_agent"] == "agent:web"

    def test_retrospective_review_threads_issue_label_provider_args(
        self,
        launcher_bundle,
    ):
        launcher_bundle.launcher.config.agents["agent:reviewer"].provider = "claude-code"
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_labels=("agent:web", "lack-of-review-redo", "verbose"),
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "--verbose" in shlex.split(command)

    def test_unset_prior_pr_is_resolved_lazily_at_launch(
        self,
        launcher_bundle,
        mock_repo_host,
        mock_events,
    ):
        """Discovery leaves prior_pr unset; the launcher resolves it here with a
        single search-only lookup (no per-PR hydration) for the one issue being
        launched, and the resolved PR flows into the prompt and launch event."""
        mock_repo_host.prs[365] = [
            PRInfo(
                number=511,
                title="Manual PR",
                url="https://github.com/test/repo/pull/511",
                branch="365-manual",
                body="hand-written, no marker",
                state="closed",
                labels=[],
            ),
            PRInfo(
                number=512,
                title="Orchestrator PR",
                url="https://github.com/test/repo/pull/512",
                branch="365-scratch",
                body=f"{ORCHESTRATOR_PR_MARKER}\nGenerated work.",
                state="closed",
                labels=[],
            ),
        ]
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            # prior_pr_number / prior_pr_url intentionally unset (discovery default).
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        # Resolved to the orchestrator-signed PR, not the manual one...
        assert result.session.pr_number == 512
        assert "Prior orchestrator PR: #512" in result.session.original_prompt
        # ...and back-filled onto the pending review so the dashboard/events see it.
        assert review.prior_pr_number == 512
        assert review.prior_pr_url == "https://github.com/test/repo/pull/512"
        event = next(
            e for e in mock_events.events if str(e.name) == str(EventName.REVIEW_STARTED)
        )
        assert event.data["prior_pr_number"] == 512
        # Exactly one cheap search call — no fan-out, no per-PR hydration.
        assert mock_repo_host.search_pr_refs_calls == [365]

    def test_preset_prior_pr_skips_lookup_at_launch(
        self,
        launcher_bundle,
        mock_repo_host,
    ):
        """When the UI preflight/queue path already resolved the prior PR, the
        launcher must not search again."""
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        assert result.session.pr_number == 512
        assert mock_repo_host.search_pr_refs_calls == []

    def test_launch_then_completion_clears_real_blocking_labels(
        self,
        launcher_bundle,
    ):
        """Production path: a blocked, trigger-labeled issue is queued with its
        real labels, launched, then approved — and the completion must clear the
        blocking labels. Regression guard proving issue_labels reach
        session.issue.labels (the synthetic pseudo-issue used to drop them, so
        get_blocking returned empty and no RemoveLabelAction was generated).
        """
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_labels=(
                "agent:web",
                "lack-of-review-redo",
                "blocked",
                "blocked-failed",
            ),
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is True
        session = result.session
        # The launched session must carry the issue's real blocking labels...
        assert "blocked" in session.issue.labels
        assert "blocked-failed" in session.issue.labels

        # ...so completion policy actually generates the removals in production.
        config = launcher_bundle.launcher.config
        config.retrospective_review_trigger_label = "lack-of-review-redo"
        config.retrospective_reviewed_label = "retrospective-reviewed"
        config.retrospective_changes_requested_label = "retrospective-changes-requested"
        actions = retrospective_review_completion_actions(
            session=session,
            status=SessionStatus.COMPLETED,
            detail={"outcome": "review_approved"},
            config=config,
            label_manager=LabelManager(config),
        )
        removed = [a.label for a in actions if isinstance(a, RemoveLabelAction)]
        assert "blocked" in removed
        assert "blocked-failed" in removed

    def test_keeps_queued_when_retrospective_terminal_already_running(
        self,
        launcher_bundle,
    ):
        launcher_bundle.session_exists_override[0] = (
            lambda name: name == "retrospective-review-365"
        )
        review = PendingRetrospectiveReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="365"),
            issue_number=365,
            issue_title="Review old implementation",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )

        result = launcher_bundle.launcher.launch_retrospective_review_session(
            review,
            active_sessions=[],
        )

        assert result.success is False
        assert result.keep_queued is True


# =============================================================================
# Rework Session Launch Tests
# =============================================================================


class TestLaunchReworkSession:
    """Tests for launch_rework_session method (lines 585, 597-599, 604-605, 611-765)."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

    def test_successful_launch_with_pr(self, session_launcher, mock_repo_host, mock_events):
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
        assert result.session.run_dir is not None
        assert result.session.run_dir.name.endswith("__coding-2")
        started = next(e for e in mock_events.events if str(e.name) == "rework.started")
        assert started.data["agent"] == "agent:web"
        assert started.data["task"] == "rework"

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

    def test_rework_launch_clears_coding_interrupted_guard_label(self, launcher_bundle):
        """Rework launch should clear interrupted coding retry guard."""
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        guard_label = launcher_bundle.launcher.config.retry.interrupted_sessions.coding_guard_label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(
            isinstance(a, RemoveLabelAction)
            and a.issue_number == 123
            and a.label == guard_label
            for a in actions
        )

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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="rework-123",
            ),
        )
        rework = PendingRework(
            issue_key=issue_key,
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = session_launcher.launch_rework_session(rework, active_sessions=[existing_session])

        assert result.success is False
        assert "Already in active sessions" in result.reason

    def test_skips_when_terminal_already_running(self, launcher_bundle):
        """Verify keeps queued when terminal exists."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "rework-123"
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is False
        assert result.keep_queued is True

    def test_updates_rework_cycle_label(self, launcher_bundle, mock_repo_host):
        """Verify rework cycle label is updated (lines 820-839)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=2,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should add rework-cycle-2 label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, AddLabelAction) and a.label == "rework-cycle-2" for a in actions)

    def test_removes_needs_rework_label(self, launcher_bundle, mock_repo_host):
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

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Should remove needs-rework label
        actions = [call.args[0] for call in launcher_bundle.action_applier.apply.call_args_list]
        assert any(isinstance(a, RemoveLabelAction) and a.label == "needs-rework" for a in actions)

    def test_rework_pr_view_changed_uses_stable_issue_key(self, launcher_bundle, mock_repo_host, mock_events):
        """PR_VIEW_CHANGED event on rework start must use stable issue_key, not str(issue_number)."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="M0-721"),
            agent_type="agent:web",
            rework_cycle=1,
            issue_number=123,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        pr_view_events = mock_events.get_events_by_name("pr.view_changed")
        assert len(pr_view_events) >= 1
        # The rework-start event removing needs-rework should use stable key
        remove_event = next(
            (e for e in pr_view_events if "needs-rework" in e.data.get("removed", [])),
            None,
        )
        assert remove_event is not None, "Expected pr.view_changed with needs-rework removal"
        assert remove_event.data["issue_key"] == "M0-721"

    def test_includes_reviewer_feedback_in_prompt(self, launcher_bundle, mock_repo_host):
        """Verify reviewer feedback is included in the agent prompt."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Please add unit tests", "user": {"login": "reviewer1"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Please add unit tests" in command
        assert "reviewer1" in command

    def test_includes_post_publish_validation_feedback_in_prompt(self, launcher_bundle, mock_repo_host):
        """Verify post-publish validation feedback is included in the rework prompt."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = []
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
            source="post_publish_validation",
            feedback="POST-PUBLISH VALIDATION FAILURE (address these issues):\n\nResolve merge conflicts.",
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "POST-PUBLISH VALIDATION FAILURE" in command
        assert "Resolve merge conflicts." in command

    def test_excludes_approved_reviews_from_feedback(self, launcher_bundle, mock_repo_host):
        """Verify APPROVED reviews are not included in feedback."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Fix the bug", "user": {"login": "alice"}},
            {"state": "APPROVED", "body": "LGTM", "user": {"login": "bob"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "Fix the bug" in command
        assert "alice" in command
        # APPROVED review should not be included
        assert "LGTM" not in command
        assert "bob" not in command

    def test_excludes_reviews_with_empty_body(self, launcher_bundle, mock_repo_host):
        """Verify reviews with empty bodies are excluded."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        # No feedback should be included (empty body)
        assert "REVIEWER FEEDBACK" not in command

    def test_no_feedback_when_no_reviews(self, launcher_bundle, mock_repo_host):
        """Verify no feedback section when there are no reviews."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = []
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" not in command

    def test_escapes_quotes_in_feedback(self, launcher_bundle, mock_repo_host):
        """Verify quotes in reviewer feedback don't break the command."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "Don't use 'eval' here", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Command should not break - the eval text should still be present
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "eval" in command

    def test_includes_commented_reviews_with_body(self, launcher_bundle, mock_repo_host):
        """Verify COMMENTED reviews with body text are included."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "COMMENTED", "body": "Consider using a helper function here", "user": {"login": "reviewer"}},
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Consider using a helper function here" in command

    def test_uses_local_feedback_file_within_cache_window(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify local feedback file is used when within cache window."""
        import json
        from datetime import datetime, timezone

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        # Don't set pr_reviews - we want to test local file is used instead

        # Create local feedback file in review session's run directory
        # Issue number is 123, so worktree is at tmp_path / "worktree-123"
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        feedback_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pr_number": 456,
            "review_issues": "Local feedback: please add tests",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "REVIEWER FEEDBACK" in command
        assert "Local feedback: please add tests" in command

    def test_falls_back_to_github_when_local_file_too_old(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify GitHub API is used when local file is outside cache window."""
        import json
        from datetime import datetime, timezone, timedelta

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]
        mock_repo_host.pr_reviews[456] = [
            {"state": "CHANGES_REQUESTED", "body": "GitHub API feedback", "user": {"login": "reviewer"}},
        ]

        # Create old local feedback file (outside cache window)
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        old_timestamp = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        feedback_data = {
            "timestamp": old_timestamp,
            "pr_number": 456,
            "review_issues": "Old local feedback",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        command = launcher_bundle.create_session_calls[0]["cmd"]
        # Should use GitHub API feedback, not old local file
        assert "GitHub API feedback" in command
        assert "Old local feedback" not in command

    def test_copies_feedback_from_review_to_rework_run_dir(self, launcher_bundle, mock_repo_host, mock_worktree_manager):
        """Verify feedback is copied from review session's run dir to rework's run dir."""
        import json
        from datetime import datetime, timezone

        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Fix", url="url", branch="123-fix", body="", state="open", labels=[])
        ]

        # Create local feedback file in review session's run directory
        worktree_path = mock_worktree_manager.tmp_path / "worktree-123"
        worktree_path.mkdir(parents=True, exist_ok=True)
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        review_run_dir = sessions_dir / "20240115-120000__review-456"
        review_run_dir.mkdir(parents=True, exist_ok=True)

        feedback_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pr_number": 456,
            "review_issues": "Copied feedback content",
        }
        (review_run_dir / "reviewer-feedback.json").write_text(json.dumps(feedback_data))

        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        result = launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert result.success is True
        # Find the rework session's run directory
        rework_dirs = [d for d in sessions_dir.iterdir() if d.name.startswith("rework-") or d.name.startswith("20")]
        # Filter to the coding session (rework creates a coding-N directory)
        coding_dirs = [d for d in rework_dirs if "coding" in d.name]
        assert len(coding_dirs) > 0, "Rework coding session directory should exist"
        rework_feedback = coding_dirs[0] / "reviewer-feedback.json"
        assert rework_feedback.exists(), "Feedback should be copied to rework run dir"
        copied_data = json.loads(rework_feedback.read_text())
        assert copied_data["review_issues"] == "Copied feedback content"


# Note: TestRunSetupCommands class deleted - tested private _run_setup_commands method.
# Setup command behavior is already tested through test_runs_setup_commands in TestLaunchIssueSession.


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

    def test_suppresses_duplicate_terminal_id_from_launch_result(
        self,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Launch wrappers must not admit duplicate terminal IDs."""
        existing = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "existing",
            branch_name="123-existing",
            run_assets=make_session_run_assets(
                tmp_path / "existing",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "duplicate",
            branch_name="123-duplicate",
            run_assets=make_session_run_assets(
                tmp_path / "duplicate",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[existing])
        session_launcher = MagicMock()
        session_launcher.launch_issue_session.return_value = LaunchResult(
            duplicate,
            True,
        )

        result = orchestrator_launch_session(sample_issue, state, session_launcher)

        assert result is duplicate
        assert state.active_sessions == [existing]

    def test_keeps_orphaned_terminal_unrestored_without_run_assets(
        self,
        launcher_bundle,
        sample_issue,
    ):
        """Launch routing does not synthesize active sessions without run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": "",
            }
        ]
        state = OrchestratorState()
        mock_restorer = MagicMock()

        result = orchestrator_launch_session(
            sample_issue,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is None
        assert state.active_sessions == []
        mock_restorer.restore_known_terminal.assert_not_called()

    def test_restores_orphaned_terminal_from_discovered_run_assets(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Launch routing re-tracks existing terminals with typed run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        run_assets = make_session_run_assets(tmp_path, session_name="issue-123")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        state = OrchestratorState()
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path,
            branch_name="123-test",
            run_assets=run_assets,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "issue-123"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_session(
            sample_issue,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is restored
        assert state.active_sessions == [restored]
        mock_restorer.restore_known_terminal.assert_called_once_with(
            issue_number=123,
            session_name="issue-123",
            run_dir=run_assets.run_dir,
            is_review=False,
            already_tracked=[],
            tab_name="",
        )


class TestOrchestratorLaunchValidationRetrySession:
    """Tests for validation retry launch wrapper."""

    def test_removes_pending_retry_after_success(self, launcher_bundle):
        """Successful retry launch removes only that issue from pending retries."""
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-123",
            branch_name="123-fix-checkout",
            original_prompt="original task",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        other_retry = PendingValidationRetry(
            issue_number=456,
            issue_title="Other",
            agent_label="agent:web",
            worktree_path="/tmp/worktree-456",
            branch_name="456-other",
            original_prompt="original task",
            validation_error="failed",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state = OrchestratorState(pending_validation_retries=[retry, other_retry])

        result = orchestrator_launch_validation_retry_session(
            retry,
            state,
            launcher_bundle.launcher,
            MagicMock(),
        )

        assert result is not None
        assert [r.issue_number for r in state.pending_validation_retries] == [456]
        assert [s.terminal_id for s in state.active_sessions] == ["issue-123"]

    def test_restores_keep_queued_retry_and_removes_pending(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Existing retry terminals are re-tracked from discovered run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "issue-123"
        run_assets = make_session_run_assets(tmp_path, session_name="issue-123")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "issue-123",
                "is_review": False,
                "session_name": "issue-123",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        retry = PendingValidationRetry(
            issue_number=123,
            issue_title="Fix checkout",
            agent_label="agent:web",
            worktree_path=str(tmp_path),
            branch_name="123-fix-checkout",
            original_prompt="original task",
            validation_error="dirty worktree",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state = OrchestratorState(pending_validation_retries=[retry])
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path,
            branch_name="123-fix-checkout",
            run_assets=run_assets,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "issue-123"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_validation_retry_session(
            retry,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is restored
        assert state.active_sessions == [restored]
        assert state.pending_validation_retries == []


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

    def test_keeps_orphaned_terminal_unrestored_without_run_assets(self, launcher_bundle):
        """Review launch routing refuses restoration without run assets."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "Review PR #456",
                "is_review": True,
                "session_name": "review-456",
                "run_dir": "",
            }
        ]
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
        mock_restorer.restore_known_terminal.return_value = []

        result = orchestrator_launch_review_session(review, state, launcher_bundle.launcher, mock_restorer)

        assert result is None
        mock_restorer.restore_known_terminal.assert_not_called()
        assert state.pending_reviews == [review]

    def test_restores_review_terminal_from_discovered_run_assets(
        self,
        launcher_bundle,
        sample_issue,
        sample_agent_config,
        tmp_path,
    ):
        """Review launch routing re-tracks keep-queued review terminals."""
        launcher_bundle.session_exists_override[0] = lambda name: name == "review-456"
        run_assets = make_session_run_assets(tmp_path, session_name="review-456")
        launcher_bundle.launcher.session_manager.runner.discover_running_sessions.return_value = [
            {
                "issue_number": 123,
                "tab_name": "Review PR #456",
                "is_review": True,
                "session_name": "review-456",
                "run_dir": str(run_assets.run_dir),
            }
        ]
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-feature",
            _issue_number=123,
        )
        state = OrchestratorState(pending_reviews=[review])
        restored = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.REVIEW),
            issue=sample_issue,
            agent_config=sample_agent_config,
            terminal_id="review-456",
            worktree_path=tmp_path,
            branch_name="123-feature",
            run_assets=run_assets,
            pr_number=456,
        )
        mock_restorer = MagicMock()
        mock_restorer.canonical_terminal_id.return_value = "review-456"
        mock_restorer.restore_known_terminal.return_value = [restored]

        result = orchestrator_launch_review_session(
            review,
            state,
            launcher_bundle.launcher,
            mock_restorer,
        )

        assert result is restored
        assert state.active_sessions == [restored]
        assert state.pending_reviews == []
        mock_restorer.restore_known_terminal.assert_called_once_with(
            issue_number=123,
            session_name="review-456",
            run_dir=run_assets.run_dir,
            is_review=True,
            already_tracked=[],
            tab_name="Review PR #456",
        )


class TestOrchestratorLaunchReworkSession:
    """Tests for orchestrator_launch_rework_session function."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

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

        def retrospective_fn(n):
            calls.append(("retrospective", n))
            return None

        def rework_fn(n):
            calls.append(("rework", n))
            return None

        def triage_fn(n):
            calls.append(("triage", n))
            return None

        session_launcher_callback(
            SessionType.ISSUE,
            123,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            triage_fn,
        )
        session_launcher_callback(
            SessionType.REVIEW,
            456,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            triage_fn,
        )
        session_launcher_callback(
            SessionType.RETROSPECTIVE_REVIEW,
            789,
            issue_fn,
            review_fn,
            retrospective_fn,
            rework_fn,
            triage_fn,
        )

        assert calls == [("issue", 123), ("review", 456), ("retrospective", 789)]

    def test_unknown_session_type_fails_fast(self):
        """Unknown session types should not silently no-op."""

        def launch_fn(_number):
            return None

        with pytest.raises(KeyError):
            session_launcher_callback(
                cast(SessionType, object()),
                123,
                launch_fn,
                launch_fn,
                launch_fn,
                launch_fn,
                launch_fn,
            )


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
        mock_session.terminal_id = "issue-123"
        mock_restorer.restore_sessions.return_value = [mock_session]

        active_sessions = []
        running = [{"tab_name": "issue-123", "issue_number": 123}]

        added = restore_running_sessions(running, active_sessions, mock_restorer)

        assert added == [mock_session]
        assert len(active_sessions) == 1
        assert active_sessions[0] == mock_session

    def test_suppresses_duplicate_terminal_ids_from_restorer(self, tmp_path):
        """Runtime restoration also goes through the active-session owner."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        agent_config = AgentConfig(prompt_path=tmp_path / "prompt.txt")
        existing = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "existing",
            branch_name="123-existing",
            run_assets=make_session_run_assets(
                tmp_path / "existing",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "duplicate",
            branch_name="123-duplicate",
            run_assets=make_session_run_assets(
                tmp_path / "duplicate",
                session_name="issue-123",
            ),
        )
        new_session = Session(
            key=SessionKey(issue=FakeIssueKey("456"), task=TaskKind.CODE),
            issue=Issue(number=456, title="Other", labels=["agent:web"]),
            agent_config=agent_config,
            terminal_id="issue-456",
            worktree_path=tmp_path / "new",
            branch_name="456-new",
            run_assets=make_session_run_assets(
                tmp_path / "new",
                session_name="issue-456",
            ),
        )
        mock_restorer = MagicMock()
        mock_restorer.restore_sessions.return_value = [duplicate, new_session]
        active_sessions = [existing]

        added = restore_running_sessions(
            [{"tab_name": "issue-123"}, {"tab_name": "issue-456"}],
            active_sessions,
            mock_restorer,
        )

        assert added == [new_session]
        assert active_sessions == [existing, new_session]


# =============================================================================
# Process Active Sessions Tests
# =============================================================================


class TestProcessActiveSessions:
    """Tests for process_active_sessions function (line 1034)."""

    def test_completed_decision_batch_applies_siblings_before_raising(self):
        """One failed completed decision must not discard the rest of the drain batch."""
        first = CompletedDecision(
            session=MagicMock(terminal_id="issue-1"),
            decision=None,
            error=RuntimeError("decide failed"),
        )
        second = CompletedDecision(
            session=MagicMock(terminal_id="issue-2"),
            decision=SessionDecision(
                status=SessionStatus.RUNNING,
                provider_success="codex",
            ),
            error=None,
        )
        provider_resilience = MagicMock()
        applied: list[str] = []

        def apply(completed: CompletedDecision) -> None:
            applied.append(completed.session.terminal_id)
            if completed.error is not None:
                raise completed.error
            assert completed.decision is not None
            _record_provider_resilience_effects(
                completed.decision,
                provider_resilience,
            )

        with pytest.raises(RuntimeError, match="decide failed"):
            _apply_completed_decisions([first, second], apply)

        assert applied == ["issue-1", "issue-2"]
        provider_resilience.record_success.assert_called_once_with("codex")

    def test_provider_resilience_effects_are_recorded_on_apply_thread(self):
        """Provider-circuit mutations happen when the drained decision is applied."""
        provider_resilience = MagicMock()

        _record_provider_resilience_effects(
            SessionDecision(
                status=SessionStatus.RUNNING,
                provider_success="codex",
            ),
            provider_resilience,
        )
        _record_provider_resilience_effects(
            SessionDecision(
                status=SessionStatus.BLOCKED,
                provider_transient_failure=ProviderTransientFailureDecision(
                    provider="claude-code",
                    error_summary="provider overloaded",
                    attempts=3,
                ),
            ),
            provider_resilience,
        )

        provider_resilience.record_success.assert_called_once_with("codex")
        provider_resilience.record_transient_failure.assert_called_once_with(
            "claude-code",
            error_summary="provider overloaded",
            attempts=3,
        )

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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
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

    def test_keeps_deferred_completion_sessions_active(self, sample_agent_config, tmp_path):
        """A RUNNING decision after observation is a deferral, not completion."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.terminated()
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.RUNNING,
            reason="Review exchange running in background; awaiting completion",
        )
        mock_completion_handler = MagicMock()
        mock_action_applier = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            worktree_manager=None,
            kill_session_fn=MagicMock(),
            config=MagicMock(),
        )

        assert state.active_sessions == [session]
        mock_completion_handler.process_completion.assert_not_called()
        mock_action_applier.apply_actions.assert_not_called()

    def test_attributes_tick_phase_to_issue_being_handled(self, sample_agent_config, tmp_path):
        """While a session's (synchronous) completion runs, the tick phase names
        the issue, so a stall during publish shows up on the dashboard as
        'active_sessions:#392' rather than a generic 'active_sessions'."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=392, title="Test", labels=["agent:backend"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("392"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-392",
            worktree_path=tmp_path / "worktree",
            branch_name="392-feature",
            run_assets=make_session_run_assets(tmp_path / "worktree", session_name="issue-392"),
        )

        state = OrchestratorState()
        state.active_sessions = [session]

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.terminated()

        captured_phase = {}

        def capture_phase(*args, **kwargs):
            # The heavy publish work happens inside decide_outcome; capture what
            # the dashboard would read if it sampled mid-stall.
            captured_phase["value"] = state.current_tick_phase
            return SessionDecision(
                status=SessionStatus.RUNNING,
                reason="deferred",
            )

        mock_controller = MagicMock()
        mock_controller.decide_outcome.side_effect = capture_phase

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=MagicMock(),
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=MagicMock(),
            config=MagicMock(),
        )

        assert captured_phase["value"] == "active_sessions:#392"

    def test_completion_event_fires_once_across_many_ticks_of_deferred_session(
        self, sample_agent_config, tmp_path
    ):
        """Regression for #6082: OBSERVATION_COMPLETION_DETECTED must fire
        exactly once per session, even when the controller keeps the session
        active across many ticks (e.g. a background review exchange that
        returns SessionStatus.RUNNING).

        This test deliberately uses a real SessionObserver — the bug lives
        at the seam between observer (re-reads completion.json each tick),
        controller (says RUNNING during deferred work), and
        process_active_sessions (keeps RUNNING sessions in active_sessions).
        Each component is individually correct; the cardinality invariant
        only emerges from running them together across ticks.
        """
        import json
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.events import EventName
        from issue_orchestrator.observation.observer import SessionObserver

        issue = Issue(number=359, title="Test", labels=["agent:backend"])
        issue_key = FakeIssueKey("359")
        worktree = tmp_path / "worktree"
        worktree.mkdir(parents=True)
        completion_dir = worktree / ".issue-orchestrator"
        completion_dir.mkdir(parents=True)
        (completion_dir / "completion.json").write_text(json.dumps({
            "session_id": "any-session-id",
            "timestamp": "2024-01-01T00:00:00Z",
            "outcome": "completed",
            "summary": "Work done",
        }))

        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-359",
            worktree_path=worktree,
            branch_name="359-feature",
            run_assets=make_session_run_assets(
                worktree,
                session_name="issue-359",
            ),
        )
        state = OrchestratorState(active_sessions=[session])

        events = MockEventSink()
        mock_session_runner = MagicMock()
        mock_session_runner.session_exists_by_name.return_value = True
        observer = SessionObserver(
            MagicMock(),
            FileSystemSessionOutput(),
            events=events,
            session_runner=mock_session_runner,
            repository_host=MagicMock(),
        )

        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.RUNNING,
            reason="Review exchange running in background; awaiting completion",
        )

        # Simulate ten observation ticks while the session lingers in a
        # deferred state — exactly the live tixmeup #359 scenario.
        for _ in range(10):
            process_active_sessions(
                state=state,
                observer=observer,
                session_controller=mock_controller,
                completion_handler=MagicMock(),
                action_applier=MagicMock(),
                worktree_manager=None,
                kill_session_fn=MagicMock(),
                config=MagicMock(),
            )

        # Session stayed active because the controller deferred…
        assert state.active_sessions == [session]
        # …and the controller saw a terminated observation each tick…
        assert mock_controller.decide_outcome.call_count == 10
        # …but the user-facing event fired exactly once.
        completion_events = events.get_events_by_name(
            EventName.OBSERVATION_COMPLETION_DETECTED
        )
        assert len(completion_events) == 1, (
            "OBSERVATION_COMPLETION_DETECTED must fire once per session, not "
            f"once per tick. Got {len(completion_events)} emissions."
        )

    def test_skips_duplicate_snapshot_entries_after_terminal_processing(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Duplicate active-session entries cannot append duplicate timeout events."""
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        duplicate = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session, duplicate])
        mock_observer = MagicMock()
        terminal_observation = SessionObservationResult.timed_out()
        mock_observer.observe_session.return_value = terminal_observation
        mock_controller = MagicMock()
        mock_controller.decide_outcome.return_value = SessionDecision(
            status=SessionStatus.TIMED_OUT,
            reason="timeout",
        )
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        mock_controller.session_output = session_output
        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=90,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        kill_session_fn = MagicMock()

        process_active_sessions(
            state=state,
            observer=mock_observer,
            session_controller=mock_controller,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            worktree_manager=None,
            kill_session_fn=kill_session_fn,
            config=MagicMock(),
        )

        assert state.active_sessions == []
        mock_observer.observe_session.assert_called_once_with(session)
        mock_controller.decide_outcome.assert_called_once()
        mock_completion_handler.process_completion.assert_called_once()
        kill_session_fn.assert_called_once_with("issue-123")

    def test_background_dispatcher_keeps_heartbeat_free_until_drained(
        self, sample_agent_config, tmp_path
    ):
        """With a background dispatcher, a slow completion decision runs off the
        tick thread: the dispatch tick returns immediately with the session
        still active and no completion applied; a later tick (after the decision
        finishes) drains it and applies handle_session_completion exactly once.
        Regression for the 153.9s synchronous-publish freeze."""
        import threading

        from issue_orchestrator.control.completion_dispatcher import (
            BackgroundCompletionDispatcher,
        )
        from issue_orchestrator.control.session_controller import SessionDecision
        from issue_orchestrator.execution.thread_background_job_runner import (
            ThreadBackgroundJobRunner,
        )
        from issue_orchestrator.observation.observation import SessionObservationResult

        issue = Issue(number=392, title="Test", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("392"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-392",
            worktree_path=tmp_path / "worktree",
            branch_name="392-feature",
            run_assets=make_session_run_assets(tmp_path / "worktree", session_name="issue-392"),
        )
        state = OrchestratorState(active_sessions=[session])

        mock_observer = MagicMock()
        mock_observer.observe_session.return_value = SessionObservationResult.timed_out()

        gate = threading.Event()

        def slow_decide(*args, **kwargs):
            gate.wait(5)  # stand in for the ~100s publish gate + push + PR
            return SessionDecision(status=SessionStatus.TIMED_OUT, reason="done")

        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None
        mock_controller = MagicMock()
        mock_controller.decide_outcome.side_effect = slow_decide
        mock_controller.session_output = session_output

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=392, title="Test", agent_type="agent:web",
                status="timed_out", runtime_minutes=90,
            ),
            should_defer_cleanup=False, pending_cleanup=None,
            should_queue_review=False, pr_url=None, pr_number=None,
        )
        kill_session_fn = MagicMock()
        runner = ThreadBackgroundJobRunner()
        dispatcher = BackgroundCompletionDispatcher(runner)

        def run_tick():
            process_active_sessions(
                state=state,
                observer=mock_observer,
                session_controller=mock_controller,
                completion_handler=mock_completion_handler,
                action_applier=MagicMock(),
                worktree_manager=None,
                kill_session_fn=kill_session_fn,
                config=MagicMock(),
                completion_dispatcher=dispatcher,
            )

        # Tick 1: dispatch only — decision runs in the background, tick returns.
        run_tick()
        assert session in state.active_sessions  # not yet completed
        mock_completion_handler.process_completion.assert_not_called()
        assert dispatcher.in_flight("issue-392") is True

        # Tick 2 (decision still running): in-flight, so no re-dispatch, no apply.
        run_tick()
        mock_controller.decide_outcome.assert_called_once()
        assert state.active_sessions == [session]

        # Decision finishes; the next tick drains and applies completion once.
        gate.set()
        assert runner.wait_until_idle(5) is True
        run_tick()
        assert state.active_sessions == []
        mock_completion_handler.process_completion.assert_called_once()
        kill_session_fn.assert_called_once_with("issue-392")


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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
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
            session_output=MagicMock(spec=SessionOutput),
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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
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
            session_output=MagicMock(spec=SessionOutput),
        )

        assert len(state.discovered_failures) == 1
        assert state.discovered_failures[0].issue_number == 123

    def test_timed_out_session_kills_terminal_before_actions(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Timeout terminalization happens before external completion actions."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        calls: list[str] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = lambda *args, **kwargs: (
            calls.append("process_completion")
            or MagicMock(
                actions=[AddLabelAction(issue_number=123, label="blocked-failed")],
                history_entry=SessionHistoryEntry(
                    issue_number=123,
                    title="Test Issue",
                    agent_type="agent:web",
                    status="timed_out",
                    runtime_minutes=90,
                ),
                should_defer_cleanup=False,
                pending_cleanup=None,
                should_queue_review=False,
                pr_url=None,
                pr_number=None,
            )
        )
        mock_action_applier = MagicMock()
        mock_action_applier.apply_all.side_effect = lambda _actions: calls.append("actions")
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.TIMED_OUT,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: calls.append("kill"),
            config=MagicMock(),
            session_output=session_output,
        )

        assert calls == ["process_completion", "kill", "actions"]
        assert state.active_sessions == []
        assert len(state.discovered_failures) == 1

    def test_completion_artifacts_use_recorded_run_dir_without_lookup(
        self,
        sample_agent_config,
        tmp_path,
        monkeypatch,
    ):
        """Completion diagnostics use the launch-recorded artifact path."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                run_id="20260525",
                session_name="coding-1",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        run_dir = session.run_dir
        analyzed: list[Path] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=123,
                title="Test Issue",
                agent_type="agent:web",
                status="timed_out",
                runtime_minutes=90,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        session_output = MagicMock(spec=SessionOutput)
        monkeypatch.setattr(
            "issue_orchestrator.control.session_completion.run_session_analysis",
            lambda path: analyzed.append(path),
        )

        handle_session_completion(
            session=session,
            status=SessionStatus.TIMED_OUT,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: None,
            config=MagicMock(),
            session_output=session_output,
        )

        session_output.find_run_dir.assert_not_called()
        session_output.read_manifest.assert_not_called()
        session_output.attach_claude_log.assert_called_once_with(run_dir)
        assert analyzed == [run_dir]

    def test_completed_session_stops_runtime_before_cleanup_actions(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """A completed agent must not remain visible as a running terminal."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        calls: list[str] = []

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = lambda *args, **kwargs: (
            calls.append("process_completion")
            or MagicMock(
                actions=[AddLabelAction(issue_number=123, label="code-reviewed")],
                history_entry=SessionHistoryEntry(
                    issue_number=123,
                    title="Test Issue",
                    agent_type="agent:web",
                    status="completed",
                    runtime_minutes=12,
                ),
                should_defer_cleanup=False,
                pending_cleanup=None,
                should_queue_review=False,
                pr_url=None,
                pr_number=None,
            )
        )
        mock_action_applier = MagicMock()
        mock_action_applier.apply_all.side_effect = lambda _actions: calls.append("actions")
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: calls.append("kill"),
            config=MagicMock(),
            session_output=session_output,
        )

        assert calls == ["process_completion", "kill", "actions"]
        assert state.active_sessions == []
        assert state.completed_today == [123]

    def test_retrospective_changes_requested_queues_coder_rework(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """Review-first changes_requested hands the issue to coder rework."""
        issue = Issue(
            number=365,
            title="Review existing work",
            labels=["agent:web", "agent:reviewer", "lack-of-review-redo"],
        )
        session = Session(
            key=SessionKey(
                issue=FakeIssueKey("365"),
                task=TaskKind.RETROSPECTIVE_REVIEW,
            ),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="retrospective-review-365",
            worktree_path=tmp_path / "worktree",
            branch_name="365-review",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="retrospective-review-365",
            ),
            agent_label="agent:reviewer",
        )
        state = OrchestratorState(active_sessions=[session])
        completion_detail = {
            "outcome": "review_changes_requested",
            "review_issues": "Add regression tests before approving.",
        }

        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.return_value = MagicMock(
            actions=[],
            history_entry=SessionHistoryEntry(
                issue_number=365,
                title="Review existing work",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=4,
            ),
            should_defer_cleanup=False,
            pending_cleanup=None,
            should_queue_review=False,
            pr_url=None,
            pr_number=None,
        )
        session_output = MagicMock(spec=SessionOutput)
        session_output.find_run_dir.return_value = None

        handle_session_completion(
            session=session,
            status=SessionStatus.COMPLETED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=MagicMock(),
            observer=MagicMock(),
            worktree_manager=None,
            kill_session_fn=lambda _name: None,
            config=MagicMock(),
            session_output=session_output,
            completion_detail=completion_detail,
        )

        assert len(state.pending_reworks) == 1
        rework = state.pending_reworks[0]
        assert rework.issue_number == 365
        assert rework.agent_type == "agent:web"
        assert rework.source == "retrospective_review"
        assert rework.feedback == "Add regression tests before approving."
        mock_completion_handler.process_completion.assert_called_once()
        assert (
            mock_completion_handler.process_completion.call_args.kwargs["completion_detail"]
            == completion_detail
        )

    def test_finished_session_already_gone_does_not_warn(
        self,
        sample_agent_config,
        tmp_path,
        caplog,
    ):
        """Adapters may report already-gone sessions while completion cleanup runs."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        session = Session(
            key=SessionKey(issue=FakeIssueKey("123"), task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )

        def already_gone(_name: str) -> None:
            raise FileNotFoundError("Session not found")

        with caplog.at_level("WARNING"):
            _terminate_finished_session(
                session,
                SessionStatus.COMPLETED,
                already_gone,
            )

        assert "Failed to stop finished session" not in caplog.text

    def test_timed_out_session_kills_terminal_when_completion_processing_fails(
        self,
        sample_agent_config,
        tmp_path,
    ):
        """A timeout cannot be restored and reprocessed if completion handling fails."""
        issue = Issue(number=123, title="Test Issue", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
        )
        state = OrchestratorState(active_sessions=[session])
        kill_session = MagicMock()
        mock_completion_handler = MagicMock()
        mock_completion_handler.process_completion.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            handle_session_completion(
                session=session,
                status=SessionStatus.TIMED_OUT,
                state=state,
                completion_handler=mock_completion_handler,
                action_applier=MagicMock(),
                observer=MagicMock(),
                worktree_manager=None,
                kill_session_fn=kill_session,
                config=MagicMock(),
                session_output=MagicMock(spec=SessionOutput),
            )

        assert state.active_sessions == []
        assert state.discovered_failures == []
        assert state.session_history == []
        assert state.immediate_cleanups == []
        kill_session.assert_called_once_with("issue-123")

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
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
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
            session_output=MagicMock(spec=SessionOutput),
        )

        assert len(state.discovered_reviews) == 1
        assert state.discovered_reviews[0].pr_number == 456

    def test_passes_blocked_reason_to_completion_handler(self, sample_agent_config, tmp_path):
        """Verify blocked_reason is forwarded to completion handler."""
        issue = Issue(number=123, title="Test", labels=["agent:web"])
        issue_key = FakeIssueKey("123")
        session = Session(
            key=SessionKey(issue=issue_key, task=TaskKind.CODE),
            issue=issue,
            agent_config=sample_agent_config,
            terminal_id="issue-123",
            worktree_path=tmp_path / "worktree",
            branch_name="123-feature",
            run_assets=make_session_run_assets(
                tmp_path / "worktree",
                session_name="issue-123",
            ),
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
                status="blocked",
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
            status=SessionStatus.BLOCKED,
            state=state,
            completion_handler=mock_completion_handler,
            action_applier=mock_action_applier,
            observer=mock_observer,
            worktree_manager=None,
            kill_session_fn=lambda x: None,
            config=config,
            session_output=MagicMock(spec=SessionOutput),
            blocked_reason="Waiting for external API",
        )

        mock_completion_handler.process_completion.assert_called_once()
        kwargs = mock_completion_handler.process_completion.call_args.kwargs
        assert kwargs["blocked_reason"] == "Waiting for external API"


# =============================================================================
# Environment Isolation Tests
# =============================================================================


class TestEnvironmentIsolation:
    """Test that sessions use proper environment isolation."""

    def test_issue_session_launches_successfully(self, launcher_bundle, sample_issue):
        """Test that issue sessions launch without HOME isolation.

        HOME isolation is disabled because Claude uses macOS keychain for
        subscription auth, which requires access to the real HOME.
        """
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        # Verify session was created
        assert len(launcher_bundle.create_session_calls) == 1

        # Verify command doesn't contain HOME override
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "export HOME=" not in command or "HOME=" not in command.split("&&")[0]

    def test_issue_session_exports_per_worktree_gradle_user_home(self, launcher_bundle, sample_issue):
        """Agent Gradle commands should use the worktree-local daemon registry."""
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert f"{GRADLE_USER_HOME_ENV}=" in command
        assert ".issue-orchestrator/tool-homes/gradle" in command

    def test_setup_commands_use_per_worktree_gradle_user_home(
        self,
        launcher_bundle,
        sample_issue,
        mock_command_runner,
    ):
        """Setup commands should share the session's isolated Gradle home."""
        launcher_bundle.launcher.config.setup_worktree = ["./gradlew tasks"]

        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        env = mock_command_runner.run_calls[0]["env"]
        assert env is not None
        assert env[GRADLE_USER_HOME_ENV].endswith(".issue-orchestrator/tool-homes/gradle")


class TestValidationOutputDir:
    """Test that sessions export VALIDATION_OUTPUT_DIR for output capture."""

    @pytest.fixture(autouse=True)
    def _no_feedback_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "issue_orchestrator.control.session_launcher.time.sleep",
            lambda _: None,
        )

    def test_issue_session_exports_validation_output_dir(self, launcher_bundle, sample_issue):
        """Test that issue sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_review_session_exports_validation_output_dir(self, launcher_bundle, mock_repo_host):
        """Test that review sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Test PR", url="url", branch="123-test", body="", state="open", labels=[])
        ]
        review = PendingReview(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            pr_number=456,
            pr_url="https://github.com/test/repo/pull/456",
            branch_name="123-test",
            _issue_number=123,
        )

        launcher_bundle.launcher.launch_review_session(review, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_rework_session_exports_validation_output_dir(self, launcher_bundle, mock_repo_host):
        """Test that rework sessions export VALIDATION_OUTPUT_DIR pointing to run_dir."""
        mock_repo_host.prs[123] = [
            PRInfo(number=456, title="Test PR", url="url", branch="123-test", body="", state="open", labels=[])
        ]
        rework = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="123"),
            agent_type="agent:web",
            rework_cycle=1,
        )

        launcher_bundle.launcher.launch_rework_session(rework, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]

        # Verify VALIDATION_OUTPUT_DIR is exported
        assert "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR=" in command

        # Verify it points to a sessions directory
        assert ".issue-orchestrator/sessions/" in command

    def test_issue_session_exports_selected_config_name(self, launcher_bundle, sample_issue):
        """Issue sessions should export selected config filename for completion commands."""
        launcher_bundle.launcher.config.config_path = Path("/tmp/main.yaml")
        launcher_bundle.launcher.launch_issue_session(sample_issue, active_sessions=[])

        assert len(launcher_bundle.create_session_calls) == 1
        command = launcher_bundle.create_session_calls[0]["cmd"]
        assert "ISSUE_ORCHESTRATOR_CONFIG_NAME='main.yaml'" in command


class TestExtraProviderArgsFromLabels:
    """Test _extra_provider_args_from_labels static helper."""

    def test_verbose_label_produces_verbose_arg(self):
        result = SessionLauncher._extra_provider_args_from_labels(["verbose", "priority:high"])  # noqa: SLF001 - unit test targets internal label parser
        assert result == {"verbose": "true"}

    def test_no_verbose_label_returns_none(self):
        result = SessionLauncher._extra_provider_args_from_labels(["priority:high", "agent:web"])  # noqa: SLF001 - unit test targets internal label parser
        assert result is None

    def test_empty_labels_returns_none(self):
        result = SessionLauncher._extra_provider_args_from_labels([])  # noqa: SLF001 - unit test targets internal label parser
        assert result is None
