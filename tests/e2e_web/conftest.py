"""Playwright e2e test fixtures for web UI testing."""

import socket
import time
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock

import pytest
import uvicorn

from issue_orchestrator.config import Config
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
    SessionHistoryEntry,
)
import issue_orchestrator.web as web_module
from issue_orchestrator.web import app


def find_free_port() -> int:
    """Find an available localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class MockOrchestratorForWeb:
    """Minimal mock orchestrator for web UI testing.

    Unlike full e2e tests, we don't need real GitHub or sessions.
    We just need enough state to render the dashboard correctly.
    """

    def __init__(self):
        self.state = OrchestratorState(
            active_sessions=[],
            session_history=[],
            completed_today=[],
            paused=False,
            priority_queue=[],
            startup_status="complete",
            startup_message="",
            cached_queue_issues=[],
            pending_reviews=[],
            dependency_problems={},
        )
        self.config = self._create_mock_config()
        self._shutdown_requested = False

    def _create_mock_config(self) -> Config:
        config = Config()
        config.repo = "test/repo"
        config.max_concurrent_sessions = 3
        config.queue_refresh_seconds = 600
        config.ui_mode = "web"
        config.web_port = 8080
        config.config_path = Path("/tmp/config.yaml")
        config.repo_root = Path("/tmp/repo")
        config.filter_label = None
        config.filter_milestone = None
        config.agents = {
            "agent:web": AgentConfig(
                prompt_path=Path("/tmp/prompt.txt"),
                worktree_base=Path("/tmp"),
                model="sonnet",
                timeout_minutes=45,
            )
        }
        return config

    def pause(self):
        self.state.paused = True

    def resume(self):
        self.state.paused = False

    def request_shutdown(self, force=False):
        self._shutdown_requested = True

    def request_refresh(self, inflight_stable_ids=None):
        pass  # No-op for UI tests

    # Helper methods for tests to populate state
    def add_active_session(
        self, issue_number: int, title: str, agent_type: str = "agent:web"
    ) -> Session:
        """Add an active session for testing."""
        issue = Issue(number=issue_number, title=title, labels=[agent_type])
        agent_config = self.config.agents["agent:web"]
        issue_key = FakeIssueKey(name=str(issue_number))
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        session = Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config,
            terminal_id=f"issue-{issue_number}",
            worktree_path=Path(f"/tmp/worktree-{issue_number}"),
            branch_name=f"feature/issue-{issue_number}",
        )
        self.state.active_sessions.append(session)
        return session

    def add_queue_issue(
        self, issue_number: int, title: str, agent_type: str = "agent:web"
    ) -> Issue:
        """Add a queued issue for testing."""
        issue = Issue(number=issue_number, title=title, labels=[agent_type])
        self.state.cached_queue_issues.append(issue)
        return issue

    def add_history_entry(
        self, issue_number: int, title: str, status: str = "completed"
    ) -> SessionHistoryEntry:
        """Add a history entry for testing."""
        entry = SessionHistoryEntry(
            issue_number=issue_number,
            title=title,
            agent_type="agent:web",
            status=status,
            runtime_minutes=15,
            pr_url=f"https://github.com/test/repo/pull/{issue_number}"
            if status == "completed"
            else "",
        )
        self.state.session_history.append(entry)
        return entry


@pytest.fixture(scope="function")
def mock_orchestrator() -> MockOrchestratorForWeb:
    """Create a fresh mock orchestrator for each test."""
    return MockOrchestratorForWeb()


class UvicornTestServer:
    """Wrapper to manage uvicorn server lifecycle in a thread."""

    def __init__(self, app, host: str, port: int):
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = None

    def start(self):
        """Start the server in a background thread."""
        self.thread = Thread(target=self.server.run, daemon=True)
        self.thread.start()
        # Wait for server to be ready
        time.sleep(0.5)

    def stop(self):
        """Stop the server."""
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture(scope="function")
def web_server(mock_orchestrator: MockOrchestratorForWeb):
    """Start web server with mock orchestrator.

    Uses function scope so each test gets a clean state.
    Runs uvicorn in a background thread.
    """
    port = find_free_port()

    # Set the global orchestrator reference (used by endpoints like /api/pause)
    original_orchestrator = web_module._orchestrator
    web_module._orchestrator = mock_orchestrator

    # Start server
    server = UvicornTestServer(app, "127.0.0.1", port)
    server.start()

    yield {
        "url": f"http://127.0.0.1:{port}",
        "port": port,
        "orchestrator": mock_orchestrator,
    }

    # Cleanup
    server.stop()
    web_module._orchestrator = original_orchestrator


# Note: pytest-playwright provides --headed option automatically
# Use: pytest tests/e2e_web --headed to run in headed mode
