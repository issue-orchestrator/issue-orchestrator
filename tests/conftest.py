"""Shared fixtures and configuration for tests."""

import pytest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, PropertyMock, patch
from issue_orchestrator.models import AgentConfig, Issue
from issue_orchestrator.config import Config, DangerousConfig
from issue_orchestrator.hookspec import hookimpl
from issue_orchestrator.ports.pull_request_tracker import PRInfo


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
    ) -> list[Issue]:
        """Return configured test issues, filtered by labels."""
        self.list_issues_calls.append({
            "labels": labels, "milestone": milestone, "state": state, "limit": limit
        })
        result = self.issues
        if labels:
            result = [i for i in result if any(l in i.labels for l in labels)]
        return result[:limit]

    def get_issue(self, issue_number: int) -> Optional[Issue]:
        """Get a specific issue."""
        for issue in self.issues:
            if issue.number == issue_number:
                return issue
        return None

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get labels for an issue."""
        return list(self.labels.get(issue_number, set()))

    # LabelManager methods
    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue."""
        self.add_label_calls.append((issue_number, label))
        self.labels.setdefault(issue_number, set()).add(label)

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue."""
        self.remove_label_calls.append((issue_number, label))
        self.labels.get(issue_number, set()).discard(label)

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

    def get_pr(self, pr_number: int) -> Optional[PRInfo]:
        """Get a specific PR."""
        for prs in self.prs.values():
            for pr in prs:
                if pr.number == pr_number:
                    return pr
        return None

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> PRInfo:
        """Create a new PR (mock)."""
        pr = PRInfo(
            number=100,
            title=title,
            url=f"https://github.com/test/repo/pull/100",
            branch=head,
            body=body,
            state="open",
            labels=[],
        )
        self.prs.setdefault(head, []).append(pr)
        return pr

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment (mock)."""
        self.comments.append({"number": issue_or_pr_number, "body": body})
        return f"https://github.com/test/repo/issues/{issue_or_pr_number}#comment"


@pytest.fixture
def mock_github_adapter():
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
    ) -> bool:
        """Track session creation."""
        self.create_session_calls.append({
            "session_id": session_id,
            "command": command,
            "working_dir": working_dir,
            "title": title,
        })
        self.sessions[session_id] = {
            "command": command,
            "working_dir": working_dir,
            "title": title,
        }
        return True

    @hookimpl
    def session_exists(self, session_id: int) -> bool:
        """Check if session was created."""
        self.session_exists_calls.append(session_id)
        if self.session_exists_override is not None:
            return self.session_exists_override
        return session_id in self.sessions

    @hookimpl
    def kill_session(self, session_id: int) -> bool:
        """Remove session."""
        self.kill_session_calls.append(session_id)
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
    def get_session_output(self, session_id: int, lines: int) -> str | None:
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
        title: str | None = None,
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
        )

    def session_exists(self, session_id: int) -> bool:
        return self._plugin.session_exists(session_id=session_id)

    def kill_session(self, session_id: int) -> None:
        self._plugin.kill_session(session_id=session_id)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines)

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
        title: str | None = None,
    ) -> bool:
        return self._plugin.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
        )

    def session_exists(self, session_id: int) -> bool:
        return self._plugin.session_exists(session_id=session_id)

    def kill_session(self, session_id: int) -> None:
        self._plugin.kill_session(session_id=session_id)

    def discover_running_sessions(self) -> list[dict]:
        return self._plugin.discover_running_sessions()

    def cleanup_idle_sessions(self) -> int:
        return self._plugin.cleanup_idle_sessions()

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        return self._plugin.get_session_output(session_id=session_id, lines=lines)


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


@pytest.fixture(autouse=True)
def patch_orchestrator_dependencies(monkeypatch, request):
    """Auto-patch orchestrator dependencies for all tests.

    This injects MockEventSink and MockSessionRunner into Orchestrator instances
    so tests don't need actual terminal backends or event infrastructure.

    The mocks are injected after __post_init__ runs, ensuring all Orchestrator
    instances get test-friendly dependencies.
    """
    # Use provided mocks if test requests them, otherwise create new ones
    if 'mock_terminal_plugin' in request.fixturenames:
        plugin = request.getfixturevalue('mock_terminal_plugin')
    else:
        plugin = MockTerminalPlugin()

    # Create shared mocks for this test
    mock_events = MockEventSink()
    mock_runner = MockSessionRunner(plugin)

    # Store original __post_init__
    from issue_orchestrator.orchestrator import Orchestrator
    original_post_init = Orchestrator.__post_init__

    def patched_post_init(self):
        # Call original first
        original_post_init(self)
        # Then inject our mocks
        self.events = mock_events
        self.runner = mock_runner
        # Store references for test assertions
        self._mock_event_sink = mock_events
        self._mock_session_runner = mock_runner

    # Patch __post_init__
    monkeypatch.setattr(Orchestrator, '__post_init__', patched_post_init)

    # Return the mocks so tests can access them
    return {'events': mock_events, 'runner': mock_runner}


@pytest.fixture
def patch_plugin_manager(patch_orchestrator_dependencies):
    """Backwards-compatible fixture for tests using the old pattern.

    Returns the MockSessionRunner, which has a .plugin property for accessing
    the underlying MockTerminalPlugin. This allows existing tests to work
    with minimal changes.

    Usage in tests:
        patch_plugin_manager.plugin.session_exists_override = False
        patch_plugin_manager.plugin.create_session_calls  # list of calls
    """
    return patch_orchestrator_dependencies['runner']


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample agent config for testing."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    return AgentConfig(
        prompt_path=prompt_file,
        worktree_base=tmp_path,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def sample_config(sample_agent_config, tmp_path):
    """Create a sample Config object for testing."""
    config = Config()
    config.agents["agent:web"] = sample_agent_config
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.ui_mode = "tmux"  # Avoid iTerm2 detection during tests
    # Use temp directory for state file to isolate tests
    config.state_file = tmp_path / ".issue-orchestrator" / "state.json"
    # Skip hook verification in tests (tests are not testing hook enforcement)
    config.dangerous = DangerousConfig(skip_verification=True, allow_unsupported_agents=True)
    return config


@pytest.fixture
def sample_orchestrator(sample_config, mock_github_adapter, patch_plugin_manager):
    """Create an Orchestrator with mock adapter injected (proper DI for testing)."""
    from issue_orchestrator.orchestrator import Orchestrator
    patch_plugin_manager.plugin.session_exists_override = False
    return Orchestrator(config=sample_config, _github_adapter=mock_github_adapter)


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
agents:
  agent:web:
    prompt: /path/to/web_prompt.txt
    worktree_base: /path/to/worktrees
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: /path/to/mobile_prompt.txt
    worktree_base: /path/to/worktrees
    model: sonnet
    timeout_minutes: 60

concurrency:
  max_sessions: 3
  session_timeout_minutes: 45

labels:
  in_progress: in-progress
  blocked: blocked
  needs_human: needs-human

repo: owner/repo
"""
    config_file = tmp_path / ".issue-orchestrator.yaml"
    config_file.write_text(config_content)
    return config_file
