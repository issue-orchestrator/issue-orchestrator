"""Playwright fixtures for Flow-first dashboard smoke tests."""

from __future__ import annotations

import socket
import time
from threading import Thread

import pytest
import uvicorn

from issue_orchestrator.domain.models import Issue
import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


def find_free_port() -> int:
    """Find a free localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FlowWebMockOrchestrator(MockOrchestratorForWeb):
    """Minimal orchestrator state builder for dashboard smoke tests."""

    def add_queue_issue(
        self,
        issue_number: int,
        title: str,
        labels: list[str] | None = None,
    ) -> None:
        issue = Issue(
            number=issue_number,
            title=title,
            labels=labels or ["agent:web"],
        )
        self.state.cached_queue_issues.append(issue)


class UvicornTestServer:
    """Manage a uvicorn server in a background thread."""

    def __init__(self, host: str, port: int) -> None:
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread: Thread | None = None

    def start(self) -> None:
        self.thread = Thread(target=self.server.run, daemon=True)
        self.thread.start()
        time.sleep(0.5)

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture
def web_server() -> dict[str, object]:
    """Run the dashboard app with a deterministic mock orchestrator."""
    orchestrator = FlowWebMockOrchestrator()
    orchestrator.add_queue_issue(408, "Flow smoke item")
    orchestrator.add_queue_issue(177, "Blocked merge item", labels=["agent:web", "blocked-needs-human"])
    port = find_free_port()

    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)

    server = UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "orchestrator": orchestrator,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)
