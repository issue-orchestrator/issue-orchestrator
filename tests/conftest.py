"""Shared fixtures and configuration for tests."""

from dataclasses import dataclass
import os
import pytest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock, patch
from fastapi.testclient import TestClient
from issue_orchestrator.domain.models import AgentConfig, Issue, Session
from issue_orchestrator.infra.config import Config, DangerousConfig
from issue_orchestrator.infra.hooks.hookspec import hookimpl
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.domain.issue_key import FakeIssueKey, IssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

TEST_ADMIN_TOKEN = "test-admin-token"
TEST_AGENT_CALLBACK_TOKEN = "test-agent-callback-token"


# =============================================================================
# Git Environment Isolation
# =============================================================================
# Prevent tests from accidentally writing git config to the main repo.
# When GIT_DIR is set, `git config` writes to that repo regardless of cwd.

@pytest.fixture(autouse=True)
def isolate_git_env(monkeypatch):
    """Strip git env vars to prevent test git commands from affecting main repo.

    This is critical because:
    - Tests create temp repos and run `git config user.email test@test.com`
    - If GIT_DIR is set in the environment, that config goes to the MAIN repo
    - This causes pollution like user.name="Test User" in the real repo

    This fixture runs for EVERY test (autouse=True) and strips these vars.
    """
    git_env_vars = [
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
    ]
    for var in git_env_vars:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_orchestrator_env(monkeypatch, tmp_path):
    """Strip orchestrator env vars and set safe defaults.

    When the orchestrator launches an agent, it exports ISSUE_ORCHESTRATOR_*
    vars (SESSION_ID, CONFIG_PATH, etc.) for coding-done/reviewer-done. If the agent then runs
    `make validate-quick` (pytest), these vars leak into unit tests and cause
    failures — e.g., CONFIG_PATH overrides test-local configs, SESSION_ID
    overrides mocked values.

    Similarly, ORCHESTRATOR_WORKTREE_BASE_BRANCH (set by e2e fixtures) can
    override test expectations in resolve_base_branch tests.

    This fixture strips them so tests always start with a clean env, then
    sets ISSUE_ORCHESTRATOR_REPO_ROOT to a temp directory so any code that
    resolves repo_root from the environment (e.g. SubprocessPlugin) never
    accidentally targets the real repo.  Tests that need a specific repo_root
    override this with their own ``monkeypatch.setenv()``.
    """
    orchestrator_env_vars = [
        "ORCHESTRATOR_WORKTREE_BASE_BRANCH",
    ]
    for var in orchestrator_env_vars:
        monkeypatch.delenv(var, raising=False)

    # Strip all ISSUE_ORCHESTRATOR_* vars (SESSION_ID, CONFIG_PATH, etc.)
    for var in list(os.environ):
        if var.startswith("ISSUE_ORCHESTRATOR_"):
            monkeypatch.delenv(var, raising=False)

    # Set a safe default REPO_ROOT so SubprocessPlugin (and anything else
    # that reads this env var) never falls back to Path.cwd() (the base repo).
    # This prevents tests from accidentally opening session_registry.sqlite
    # in the real repo's state dir.
    safe_repo = tmp_path / "isolated-repo-root"
    safe_repo.mkdir(exist_ok=True)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(safe_repo))


@pytest.fixture(autouse=True)
def reset_control_api_token():
    """Reset process-wide browser auth and bearer-token enforcement.

    ``ControlAPIServer.start`` (and tests that explicitly call
    ``configure_api_token``) install process-wide tokens on the
    ``control_app`` module. Dashboard auth has a separate process-wide
    token. Without an autouse reset, leftover tokens from an earlier
    test cause unrelated TestClient calls to return 401 instead of the
    expected status. See security issue #5987 (F3) + #6017 P3.
    """
    try:
        from issue_orchestrator.entrypoints.control_api import configure_api_token
        from issue_orchestrator.entrypoints.web import configure_dashboard_admin_token
        from issue_orchestrator.infra import browser_session
    except Exception:
        yield
        return
    configure_api_token(None, agent_callback=None)
    configure_dashboard_admin_token(None)
    browser_session.shutdown()
    try:
        yield
    finally:
        configure_api_token(None, agent_callback=None)
        configure_dashboard_admin_token(None)
        browser_session.shutdown()


@dataclass(frozen=True)
class FakeBrowserAuth:
    """Deterministic test auth that exercises the real browser middleware.

    This is the "semi-enabled" mode for route/UI tests: no real token
    file and no manual operator login, but requests still pass through
    bearer-token, session-cookie, CSRF, and SSE-token checks.
    """

    admin_token: str = TEST_ADMIN_TOKEN
    agent_callback_token: str = TEST_AGENT_CALLBACK_TOKEN

    def login(self, client: TestClient) -> str:
        """Log the TestClient in and return the session's CSRF token."""
        from issue_orchestrator.infra import browser_session

        response = client.post("/login", json={"token": self.admin_token})
        assert response.status_code == 200, response.text
        session_id = client.cookies.get(browser_session.SESSION_COOKIE)
        assert session_id, "login did not set browser session cookie"
        csrf = browser_session.get_csrf_token(session_id)
        assert csrf, "login session did not yield a CSRF token"
        return csrf

    def csrf_headers(self, client: TestClient) -> dict[str, str]:
        """Return the X-CSRF-Token header for a logged-in TestClient."""
        from issue_orchestrator.infra import browser_session

        session_id = client.cookies.get(browser_session.SESSION_COOKIE)
        assert session_id, "client must be logged in before requesting CSRF headers"
        csrf = browser_session.get_csrf_token(session_id)
        assert csrf, "logged-in client did not yield a CSRF token"
        return {browser_session.CSRF_HEADER: csrf}

    def bearer_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}


@pytest.fixture
def fake_browser_auth() -> FakeBrowserAuth:
    """Enable deterministic browser auth across Control API and Dashboard.

    Use this for tests that should catch auth wiring regressions without
    depending on ``~/.issue-orchestrator/api-token``.
    """
    from issue_orchestrator.entrypoints.control_api import configure_api_token
    from issue_orchestrator.entrypoints.web import configure_dashboard_admin_token
    from issue_orchestrator.infra import browser_session

    browser_session.shutdown()
    browser_session.initialize(admin_token=TEST_ADMIN_TOKEN)
    configure_api_token(TEST_ADMIN_TOKEN, agent_callback=TEST_AGENT_CALLBACK_TOKEN)
    configure_dashboard_admin_token(TEST_ADMIN_TOKEN)
    return FakeBrowserAuth()


@pytest.fixture
def auth_enabled_control_client(fake_browser_auth: FakeBrowserAuth) -> TestClient:
    """Control API TestClient with auth enabled but no browser login."""
    from issue_orchestrator.entrypoints.control_api import control_app

    return TestClient(control_app)


@pytest.fixture
def auth_enabled_dashboard_client(fake_browser_auth: FakeBrowserAuth) -> TestClient:
    """Dashboard TestClient with auth enabled but no browser login."""
    from issue_orchestrator.entrypoints.web import app

    return TestClient(app)


@pytest.fixture
def logged_in_dashboard_client(
    auth_enabled_dashboard_client: TestClient,
    fake_browser_auth: FakeBrowserAuth,
) -> TestClient:
    """Dashboard TestClient with auth enabled and a valid browser session."""
    fake_browser_auth.login(auth_enabled_dashboard_client)
    return auth_enabled_dashboard_client


class MockGitHubAdapter:
    """Mock GitHub adapter implementing port interfaces for testing.

    This is the proper way to test hexagonal architecture - inject a mock
    adapter rather than patching individual functions.
    """

    def __init__(self):
        # Storage for test data
        self.issues: list[Issue] = []
        self.labels: dict[int, set[str]] = {}  # issue_number -> labels
        self.prs: dict[str, list[PRInfo]] = {}  # branch -> PRs
        self.comments: list[dict] = []
        self.pr_reviews: dict[int, list[dict]] = {}  # pr_number -> reviews
        self.close_pr_calls: list[int] = []

        # Call tracking for assertions
        self.add_label_calls: list[tuple] = []
        self.remove_label_calls: list[tuple] = []
        self.list_issues_calls: list[dict] = []
        self.get_prs_calls: list[dict] = []

    # IssueRepository methods
    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
    ) -> list[Issue]:
        """Return configured test issues, filtered by labels."""
        self.list_issues_calls.append({
            "labels": labels, "milestone": milestone, "state": state, "limit": limit,
            "required_stable_ids": required_stable_ids,
        })
        result = self.issues
        if labels:
            result = [i for i in result if any(l in i.labels for l in labels)]
        return result[:limit]

    def list_issues_delta(
        self,
        *,
        since: str,
        limit: int = 100,
    ) -> tuple[list[Issue], str | None]:
        """Return issues updated since watermark (mock implementation)."""
        return self.issues[:limit], None

    def get_issue(self, issue_number: int) -> Optional[Issue]:
        """Get a specific issue."""
        for issue in self.issues:
            if issue.number == issue_number:
                return issue
        return None

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue."""
        issue = self.get_issue(issue_number)
        return issue.state if issue else None

    def create_issue_key(self, issue_number: int) -> IssueKey:
        """Create an IssueKey for testing."""
        return FakeIssueKey(name=str(issue_number))

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get labels for an issue."""
        return list(self.labels.get(issue_number, set()))

    # LabelManager methods
    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)
        pr = self.get_pr(issue_number)
        if pr and label not in pr.labels:
            pr.labels.append(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue."""
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)
        pr = self.get_pr(issue_number)
        if pr and label in pr.labels:
            pr.labels.remove(label)

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if issue has a specific label."""
        return label in self.labels.get(issue_number, set())

    # PRRepository methods
    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get PRs for a branch."""
        self.get_prs_calls.append({"branch": branch, "state": state})
        return self.prs.get(branch, [])

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get PRs with a specific label."""
        result = []
        for prs in self.prs.values():
            for pr in prs:
                if label in pr.labels:
                    result.append(pr)
        return result

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get PRs for an issue by matching branch prefix or title."""
        prefix = f"{issue_number}-"
        result: list[PRInfo] = []
        for branch, prs in self.prs.items():
            for pr in prs:
                if state != "all" and pr.state.lower() != state.lower():
                    continue
                if branch.startswith(prefix) or f"#{issue_number}" in pr.title:
                    result.append(pr)
        return result

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List all PRs."""
        result = []
        for prs in self.prs.values():
            for pr in prs:
                if state == "all" or pr.state.lower() == state.lower():
                    result.append(pr)
        return result[:limit]

    def get_pr(self, pr_number: int) -> Optional[PRInfo]:
        """Get a specific PR."""
        for prs in self.prs.values():
            for pr in prs:
                if pr.number == pr_number:
                    return pr
        return None

    def create_pr(self, title: str, body: str, head: str, base: str = "main", draft: bool | None = None) -> PRInfo:
        """Create a new PR (mock)."""
        pr = PRInfo(
            number=100,
            title=title,
            url=f"https://github.com/test/repo/pull/100",
            branch=head,
            body=body,
            state="open",
            labels=[],
            draft=draft,
        )
        self.prs.setdefault(head, []).append(pr)
        return pr

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment (mock)."""
        self.comments.append({"number": issue_or_pr_number, "body": body})
        return f"https://github.com/test/repo/issues/{issue_or_pr_number}#comment"

    def get_pr_reviews(self, pr_number: int) -> list[dict]:
        """Get reviews for a PR (mock)."""
        return self.pr_reviews.get(pr_number, [])

    def close_pr(self, pr_number: int) -> None:
        """Close a PR (mock)."""
        self.close_pr_calls.append(pr_number)
        pr = self.get_pr(pr_number)
        if pr is not None:
            pr.state = "closed"

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict | None:
        """Create a new issue (mock).

        Returns a dict with issue data (number, html_url).
        """
        # Generate next issue number
        existing_numbers = [i.number for i in self.issues]
        next_number = max(existing_numbers) + 1 if existing_numbers else 1

        issue = Issue(
            number=next_number,
            title=title,
            body=body,
            labels=labels or [],
            milestone_number=milestone,
        )
        self.issues.append(issue)
        return {"number": next_number, "html_url": f"https://github.com/test/repo/issues/{next_number}"}

    def list_milestones(self, state: str = "open") -> list[dict]:
        return getattr(self, "milestones", [])

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict | None:
        milestones = getattr(self, "milestones", [])
        number = len(milestones) + 1
        milestone = {
            "number": number,
            "title": title,
            "description": description,
            "due_on": due_on,
            "state": state,
        }
        milestones.append(milestone)
        self.milestones = milestones
        return milestone

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> None:
        issue = self.get_issue(issue_number)
        if issue is None:
            return
        issue.milestone_number = milestone
        if milestone is None:
            issue.milestone = None
            return
        for entry in getattr(self, "milestones", []):
            if entry.get("number") == milestone:
                issue.milestone = entry.get("title")
                return


@pytest.fixture
def mock_repository_host():
    """Create a mock GitHub adapter for testing."""
    return MockGitHubAdapter()


class MockTerminalPlugin:
    """Mock terminal plugin for testing.

    Implements terminal hooks and tracks calls for test assertions.
    """

    def __init__(self):
        self.sessions: dict[int, dict] = {}
        self.create_session_calls = []
        self.session_exists_calls = []
        self.kill_session_calls = []
        # Control behavior for tests
        self.session_exists_override = None  # Set to True/False to override

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        """Track session creation."""
        self.create_session_calls.append({
            "session_id": session_id,
            "session_name": session_name,
            "command": command,
            "working_dir": working_dir,
            "title": title,
        })
        self.sessions[session_id] = {
            "command": command,
            "working_dir": working_dir,
            "title": title,
            "session_name": session_name,
        }
        return True

    @hookimpl
    def session_exists(self, session_id: int, session_name: str) -> bool:
        """Check if session was created."""
        self.session_exists_calls.append((session_id, session_name))
        if self.session_exists_override is not None:
            return self.session_exists_override
        return session_id in self.sessions

    @hookimpl
    def kill_session(self, session_id: int, session_name: str) -> bool:
        """Remove session."""
        self.kill_session_calls.append((session_id, session_name))
        self.sessions.pop(session_id, None)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict]:
        """Return empty list for tests."""
        return []

    @hookimpl
    def cleanup_idle_sessions(self) -> int:
        """Return 0 for tests."""
        return 0

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        """Return None for tests."""
        return None


class MockPluginManager:
    """Mock plugin manager for testing.

    Wraps MockTerminalPlugin with the same interface as PluginManager.
    """

    def __init__(self, plugin: MockTerminalPlugin | None = None):
        self._plugin = plugin or MockTerminalPlugin()

    @property
    def plugin(self) -> MockTerminalPlugin:
        """Access the underlying mock plugin for assertions."""
        return self._plugin

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return self._plugin.session_exists(session_id=session_id, session_name=session_name)

    def kill_session(self, session_id: int, session_name: str) -> None:
        self._plugin.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines, session_name=session_name)

    def emit(self, event: str, data: dict | None = None) -> None:
        """Emit a trace event (no-op for mock, but tracks calls)."""
        if not hasattr(self, '_emit_calls'):
            self._emit_calls = []
        self._emit_calls.append((event, data or {}))


class MockEventSink:
    """Mock EventSink for testing.

    Implements the EventSink protocol and tracks published events.
    """

    def __init__(self):
        self.events: list = []

    def publish(self, event) -> None:
        """Record the event for test assertions."""
        self.events.append(event)

    def get_events_by_name(self, name: str) -> list:
        """Get all events with the given name."""
        return [e for e in self.events if e.name == name]

    def clear(self) -> None:
        """Clear recorded events."""
        self.events.clear()


class MockSessionRunner:
    """Mock SessionRunner for testing.

    Implements the SessionRunner protocol and tracks calls.
    """

    def __init__(self, plugin: MockTerminalPlugin | None = None):
        self._plugin = plugin or MockTerminalPlugin()

    @property
    def plugin(self) -> MockTerminalPlugin:
        """Access the underlying mock plugin for assertions."""
        return self._plugin

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
            session_name=session_name,
        )

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return self._plugin.session_exists(session_id=session_id, session_name=session_name)

    def kill_session(self, session_id: int, session_name: str) -> None:
        self._plugin.kill_session(session_id=session_id, session_name=session_name)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines, session_name=session_name)

    def session_exists_by_name(self, session_name: str) -> bool:
        return False

    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool:
        return False

    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        return False

    def focus_session(self, session_id: int, session_name: str) -> bool:
        return False

    def on_orchestrator_startup(self) -> None:
        """No-op for mock."""
        pass

    def on_orchestrator_shutdown(self) -> None:
        """No-op for mock."""
        pass


@pytest.fixture
def mock_terminal_plugin():
    """Create a mock terminal plugin for testing."""
    return MockTerminalPlugin()


@pytest.fixture
def mock_plugin_manager(mock_terminal_plugin):
    """Create a mock plugin manager for testing (legacy, use mock_session_runner)."""
    return MockPluginManager(mock_terminal_plugin)


@pytest.fixture
def mock_event_sink():
    """Create a mock EventSink for testing."""
    return MockEventSink()


@pytest.fixture
def mock_session_runner(mock_terminal_plugin):
    """Create a mock SessionRunner for testing."""
    return MockSessionRunner(mock_terminal_plugin)


def _build_null_queue_cache_store():
    """Create a QueueCacheStore mock that returns empty data (cold start)."""
    store = MagicMock()
    store.load_issues.return_value = []
    store.load_watermark.return_value = None
    return store


def build_test_orchestrator_deps(
    config,
    repo_host,
    events,
    runner,
    worktree_manager,
    working_copy=None,
    *,
    session_controller=None,
    label_sync=None,
    fact_gatherer=None,
    planner=None,
    session_manager=None,
    action_applier=None,
    claim_manager=None,
    lease_renewer=None,
    timeline_reader=None,
    timeline_writer=None,
):
    """Factory function to create OrchestratorDeps for testing.

    This creates properly wired control components with injected mocks,
    enabling explicit dependency injection. Returns an OrchestratorDeps
    frozen dataclass (no nulls, no optionals).

    Args:
        config: Config object
        repo_host: Repository host (MockGitHubAdapter or similar)
        events: EventSink (MockEventSink or similar)
        runner: SessionRunner (MockSessionRunner or similar)
        worktree_manager: WorktreeManager mock
        working_copy: Optional WorkingCopy (defaults to GitWorkingCopy)
        session_controller: Optional override for SessionController (for testing)
        label_sync: Optional override for LabelSync (for testing)
        fact_gatherer: Optional override for FactGatherer (for testing)
        planner: Optional override for Planner (for testing)
        session_manager: Optional override for SessionManager (for testing)
        action_applier: Optional override for ActionApplier (for testing)

    Returns:
        OrchestratorDeps with all components wired
    """
    from issue_orchestrator.control.scheduler import Scheduler
    from issue_orchestrator.control.planner import Planner
    from issue_orchestrator.control.session_manager import SessionManager
    from issue_orchestrator.control.action_applier import ActionApplier
    from issue_orchestrator.control.fact_gatherer import FactGatherer
    from issue_orchestrator.control.state_machine_manager import StateMachineManager
    from issue_orchestrator.control.session_controller import SessionController
    from issue_orchestrator.control.completion_processor import CompletionProcessor
    from issue_orchestrator.control.pr_scanner import PRScanner
    from issue_orchestrator.control.health_gate import HealthGate
    from issue_orchestrator.control.session_restorer import SessionRestorer
    from issue_orchestrator.control.label_sync import LabelSync
    from issue_orchestrator.control.orchestrator_deps import OrchestratorDeps
    from issue_orchestrator.events import EventHub
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
    from issue_orchestrator.execution.command_runner import LocalCommandRunner
    from unittest.mock import MagicMock, AsyncMock

    if working_copy is None:
        working_copy = GitWorkingCopy()
    command_runner = LocalCommandRunner()

    # Create control components with injected mocks
    # Use provided overrides or create defaults
    scheduler = Scheduler(config=config)
    _planner = planner or Planner(config=config, scheduler=scheduler)
    _session_manager = session_manager or SessionManager(runner=runner, events=events, config=config)
    _fact_gatherer = fact_gatherer or FactGatherer(config=config, repository_host=repo_host, events=events)
    state_machine_manager = StateMachineManager(config=config)

    session_output = FileSystemSessionOutput()
    from issue_orchestrator.execution.persistent_review_exchange_runner import (
        PersistentReviewExchangeRunner,
    )
    completion_processor = CompletionProcessor(
        label_adapter=repo_host,
        pr_adapter=repo_host,
        git_adapter=working_copy,
        event_bus=None,
        session_output=session_output,
        review_exchange_runner=PersistentReviewExchangeRunner(session_output),
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
        config=config,
    )
    _session_controller = session_controller or SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation and config.validation.cmd else None,
        validation_cmd=config.validation.cmd if config.validation else None,
        validation_timeout_seconds=config.validation.timeout_seconds if config.validation else 300,
        max_validation_retries=config.retry.max_validation_retries,
    )
    pr_scanner = PRScanner(
        config=config,
        repository=repo_host,
        events=events,
    )
    fresh_reader = MagicMock()
    fresh_reader.read_issue_labels.return_value = []
    e2e_issue_tracker = MagicMock()

    _action_applier = action_applier or ActionApplier(
        labels=repo_host,
        sessions=_session_manager,
        events=events,
        repository_host=repo_host,
        worktree_manager=worktree_manager,
        fresh_issue_reader=fresh_reader,
        reconcile=False,
    )

    health_gate = HealthGate(
        max_concurrent_sessions=config.max_concurrent_sessions,
        rate_limit_threshold=100,
    )

    session_restorer = SessionRestorer(
        config=config,
        repository_host=repo_host,
        working_copy=working_copy,
    )

    _label_sync = label_sync or LabelSync(labels=repo_host, events=events, pr_tracker=repo_host)
    event_hub = EventHub()
    # Create manifest downloader for triage sessions
    from issue_orchestrator.execution.triage_downloader import TriageDownloader
    manifest_downloader = TriageDownloader(
        repository_host=repo_host,
        command_runner=command_runner,
    )

    # Create claim components (NullClaimManager by default for tests)
    from issue_orchestrator.ports.claim_manager import NullClaimManager
    from issue_orchestrator.domain.lease_config import LeaseConfig
    from issue_orchestrator.control.claim_gate import ClaimGate
    from issue_orchestrator.control.lease_renewer import LeaseRenewer

    lease_config = LeaseConfig.for_testing()
    claim_manager = claim_manager or NullClaimManager()
    claim_gate = ClaimGate(claim_manager=claim_manager, events=events)
    lease_renewer = lease_renewer or LeaseRenewer(
        claim_manager=claim_manager,
        events=events,
        config=lease_config,
    )

    # Create async completion components for testing
    from issue_orchestrator.control.completion_observer import CompletionObserver
    from issue_orchestrator.control.publish_executor import PublishJobExecutor, ExecutorConfig

    completion_observer = CompletionObserver(session_output=session_output)
    executor_config = ExecutorConfig(max_workers=1)
    publish_executor = PublishJobExecutor(
        completion_processor=completion_processor,
        events=events,
        config=executor_config,
    )
    from issue_orchestrator.execution.goal_pilot_store import SqliteGoalPilotStore
    goal_pilot_store = SqliteGoalPilotStore(repo_root=config.repo_root)
    from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
    from issue_orchestrator.ports import (
        InMemoryProviderCircuitStore,
        NullTimelineReader,
        NullTimelineStore,
        NullTimelineWriter,
    )
    provider_resilience = ProviderResilienceManager(
        config.provider_resilience,
        store=InMemoryProviderCircuitStore(),
        events=events,
    )
    timeline_reader = timeline_reader or NullTimelineReader()
    timeline_writer = timeline_writer or NullTimelineWriter()

    from issue_orchestrator.control.label_manager import LabelManager
    from issue_orchestrator.control.infra_services import InfraServices
    from issue_orchestrator.control.publish_recovery import PublishRecoveryService
    from issue_orchestrator.execution.label_store import LabelStore

    label_manager = LabelManager(config)
    label_store = LabelStore(config.repo_root / ".issue-orchestrator" / "label_store.sqlite")

    _action_applier.claim_gate = claim_gate
    # build_test_orchestrator_deps() returns deps without a live Orchestrator state.
    # Use an explicit no-op lease lookup so claim verification behavior is predictable
    # for tests that consume deps directly instead of relying on runtime wiring.
    _action_applier.lease_id_lookup = lambda _issue_number: None

    infra_services = InfraServices(
        label_manager=label_manager,
        label_store=label_store,
        queue_cache_store=_build_null_queue_cache_store(),
        provider_resilience=provider_resilience,
        timeline_reader=timeline_reader,
        timeline_store=NullTimelineStore(),
        timeline_writer=timeline_writer,
        goal_pilot_store=goal_pilot_store,
    )

    publish_recovery = PublishRecoveryService(
        repository_host=repo_host,
        publish_executor=publish_executor,
        label_manager=label_manager,
        fresh_issue_reader=fresh_reader,
        action_applier=action_applier,
    )

    return OrchestratorDeps(
        events=events,
        runner=runner,
        repository_host=repo_host,
        e2e_issue_tracker=e2e_issue_tracker,
        fresh_issue_reader=fresh_reader,
        event_hub=event_hub,
        planner=_planner,
        session_manager=_session_manager,
        label_sync=_label_sync,
        action_applier=_action_applier,
        fact_gatherer=_fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        command_runner=command_runner,
        manifest_downloader=manifest_downloader,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=_session_controller,
        health_gate=health_gate,
        session_output=session_output,
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        completion_observer=completion_observer,
        publish_executor=publish_executor,
        publish_recovery=publish_recovery,
        services=infra_services,
    )


# NOTE: The autouse patch_orchestrator_dependencies fixture has been removed.
# Tests now use explicit DI via create_test_orchestrator() or build_test_orchestrator_deps().
# See tests/unit/test_orchestrator.py::create_test_orchestrator() for the pattern.
#
# If you need the legacy patching behavior for backward compatibility, use:
#   @pytest.fixture
#   def patched_orchestrator(monkeypatch):
#       # ... patching code here ...


@pytest.fixture
def patch_plugin_manager():
    """Create a MockSessionRunner for test assertions.

    DEPRECATED: Prefer using create_test_orchestrator(runner=runner) instead.
    This fixture exists for backward compatibility with tests that expect
    patch_plugin_manager.plugin to be the shared plugin instance.
    """
    return MockSessionRunner()


@pytest.fixture
def explicit_orchestrator_deps(request):
    """Create Orchestrator with all dependencies explicitly injected (no fallbacks).

    Use this fixture when you want full control over all dependencies without
    relying on __post_init__ fallbacks. This is the preferred pattern for new tests.

    Usage:
        def test_something(explicit_orchestrator_deps, sample_config):
            deps = explicit_orchestrator_deps(sample_config, mock_repo_host, mock_wt_manager)
            orchestrator = Orchestrator(
                config=sample_config,
                _repository_host=mock_repo_host,
                **deps,
            )
    """
    # Use provided mocks if test requests them, otherwise create new ones
    if 'mock_terminal_plugin' in request.fixturenames:
        plugin = request.getfixturevalue('mock_terminal_plugin')
    else:
        plugin = MockTerminalPlugin()

    if 'mock_repository_host' in request.fixturenames:
        default_repo_host = request.getfixturevalue('mock_repository_host')
    else:
        default_repo_host = MockGitHubAdapter()

    mock_events = MockEventSink()
    mock_runner = MockSessionRunner(plugin)

    def create_deps(config, repo_host=None, worktree_manager=None):
        """Create all dependencies for Orchestrator constructor."""
        rh = repo_host or default_repo_host
        wm = worktree_manager or MagicMock()
        return build_test_orchestrator_deps(config, rh, mock_events, mock_runner, wm)

    return create_deps


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample agent config for testing."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    return AgentConfig(
        prompt_path=prompt_file,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def sample_config(sample_agent_config, tmp_path):
    """Create a sample Config object for testing."""
    config = Config()
    config.repo = "test/repo"  # Required for session launching
    config.repo_root = tmp_path  # Set repo_root for worktree operations
    config.worktree_base = tmp_path  # Top-level worktree_base (no per-agent)
    config.agents["agent:web"] = sample_agent_config
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.ui_mode = "tmux"
    config.setup_worktree = []
    # Use temp directory for state file to isolate tests
    config.state_file = tmp_path / ".issue-orchestrator" / "state.json"
    # Tests are not exercising hook enforcement.
    config.dangerous = DangerousConfig(allow_unsupported_agents=True)
    return config


@pytest.fixture
def sample_orchestrator(sample_config, mock_repository_host):
    """Create an Orchestrator with all dependencies explicitly injected.

    Uses the explicit DI pattern - no autouse fixture patching needed.
    """
    from issue_orchestrator.infra.orchestrator import Orchestrator
    from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
    from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

    runner = MockSessionRunner()
    runner.plugin.session_exists_override = False
    wt_manager = GitWorktreeManager()
    wc = GitWorkingCopy()

    deps = build_test_orchestrator_deps(
        sample_config,
        mock_repository_host,
        MockEventSink(),
        runner,
        wt_manager,
        working_copy=wc,
    )

    return Orchestrator(config=sample_config, deps=deps)


@pytest.fixture
def sample_issues():
    """Create sample issues for testing."""
    return [
        Issue(
            number=1,
            title="High priority task",
            labels=["priority:high", "agent:web"],
            body="This is a high priority issue",
        ),
        Issue(
            number=2,
            title="Medium priority task",
            labels=["priority:medium", "agent:web"],
            body="This is a medium priority issue",
        ),
        Issue(
            number=3,
            title="Low priority task",
            labels=["priority:low", "agent:mobile"],
            body="This is a low priority issue",
        ),
        Issue(
            number=4,
            title="Blocked issue",
            labels=["blocked", "agent:web"],
            body="This issue is blocked by #1",
        ),
        Issue(
            number=5,
            title="In-progress issue",
            labels=["in-progress", "agent:web"],
            body="Currently being worked on",
        ),
    ]


@pytest.fixture
def make_session(sample_agent_config, tmp_path):
    """Factory fixture to create Session objects with proper SessionKey.

    Usage:
        def test_something(make_session):
            session = make_session(issue_number=123)
            session = make_session(issue_number=456, task=TaskKind.REVIEW)
    """
    def _make_session(
        issue_number: int = 123,
        issue_title: str = "Test Issue",
        issue_labels: list[str] | None = None,
        task: TaskKind = TaskKind.CODE,
        repo: str = "test/repo",
        terminal_id: str | None = None,
        branch_name: str | None = None,
        worktree_path: Path | None = None,
        agent_config: AgentConfig | None = None,
    ) -> Session:
        issue = Issue(
            number=issue_number,
            title=issue_title,
            labels=issue_labels or [],
        )
        issue_key = FakeIssueKey(name=str(issue_number))
        session_key = SessionKey(issue=issue_key, task=task)

        # Generate defaults based on task type
        if terminal_id is None:
            if task == TaskKind.REVIEW:
                terminal_id = f"review-{issue_number}"
            elif task == TaskKind.REWORK:
                terminal_id = f"rework-{issue_number}"
            else:
                terminal_id = f"issue-{issue_number}"

        if branch_name is None:
            branch_name = f"issue-{issue_number}"

        if worktree_path is None:
            worktree_path = tmp_path / f"worktree-{issue_number}"
            worktree_path.mkdir(parents=True, exist_ok=True)

        return Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config or sample_agent_config,
            terminal_id=terminal_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

    return _make_session


@pytest.fixture
def sample_issue_with_dependencies():
    """Create issues with various dependency mentions for testing."""
    return [
        Issue(
            number=101,
            title="First issue",
            labels=["priority:high"],
            body="This is the first issue",
        ),
        Issue(
            number=102,
            title="Depends on first",
            labels=["priority:medium"],
            body="This is blocked by #101",
        ),
        Issue(
            number=103,
            title="Multiple dependencies",
            labels=["priority:low"],
            body="Blocked by #101 and depends on #102",
        ),
        Issue(
            number=104,
            title="After implementation",
            labels=["priority:medium"],
            body="This should be done after #101",
        ),
        Issue(
            number=105,
            title="Requires other work",
            labels=["priority:high"],
            body="Requires #101 and #102 to be completed",
        ),
        Issue(
            number=106,
            title="Waiting for someone",
            labels=["priority:low"],
            body="Waiting for #104 to complete before starting",
        ),
    ]


@pytest.fixture
def mock_github_api():
    """Create a mock GitHub API object."""
    mock = MagicMock()
    mock.get_issues.return_value = []
    mock.add_label.return_value = None
    mock.remove_label.return_value = None
    return mock


@pytest.fixture
def mock_config_yaml(tmp_path):
    """Create a temporary config YAML file."""
    config_content = """
worktrees:
  base: /path/to/worktrees

agents:
  agent:web:
    prompt: /path/to/web_prompt.txt
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: /path/to/mobile_prompt.txt
    model: sonnet
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 3
    session_timeout_minutes: 45

labels:
  in_progress: in-progress
  blocked: blocked
  needs_human: needs-human

repo:
  name: owner/repo
"""
    config_file = tmp_path / ".issue-orchestrator.yaml"
    config_file.write_text(config_content)
    return config_file
