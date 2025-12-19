"""Shared fixtures and configuration for tests."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch
from issue_orchestrator.models import AgentConfig, Issue
from issue_orchestrator.config import Config
from issue_orchestrator.hookspec import hookimpl


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


@pytest.fixture
def mock_terminal_plugin():
    """Create a mock terminal plugin for testing."""
    return MockTerminalPlugin()


@pytest.fixture
def mock_plugin_manager(mock_terminal_plugin):
    """Create a mock plugin manager for testing."""
    return MockPluginManager(mock_terminal_plugin)


@pytest.fixture(autouse=True)
def patch_plugin_manager(monkeypatch, request):
    """Auto-patch the plugin manager for all tests.

    This replaces the real PluginManager with MockPluginManager so tests
    don't need actual tmux/iTerm2 installed.

    Tests can inject custom behavior by defining a 'mock_terminal_plugin' fixture
    or by accessing orchestrator._mock_plugin_manager.plugin after creation.
    """
    # Use provided mock_terminal_plugin if test requests it, otherwise create new
    if 'mock_terminal_plugin' in request.fixturenames:
        plugin = request.getfixturevalue('mock_terminal_plugin')
    else:
        plugin = MockTerminalPlugin()

    mock_pm = MockPluginManager(plugin)

    def mock_plugins_property(self):
        # Store on instance so tests can access it
        if not hasattr(self, '_mock_plugin_manager'):
            self._mock_plugin_manager = mock_pm
        return self._mock_plugin_manager

    # Patch the _plugins property on Orchestrator
    from issue_orchestrator.orchestrator import Orchestrator
    monkeypatch.setattr(
        Orchestrator,
        '_plugins',
        property(mock_plugins_property),
    )

    # Return the mock so tests can access it
    return mock_pm


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
def sample_config(sample_agent_config):
    """Create a sample Config object for testing."""
    config = Config()
    config.agents["agent:web"] = sample_agent_config
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.ui_mode = "tmux"  # Avoid iTerm2 detection during tests
    return config


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
