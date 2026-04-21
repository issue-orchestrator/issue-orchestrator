"""Playwright fixtures for Flow-first dashboard smoke tests."""

from __future__ import annotations

from pathlib import Path
import socket
import time
from threading import Thread
from unittest.mock import MagicMock

import pytest
import uvicorn

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader
from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from issue_orchestrator.ports.timeline_store import TimelineRecord
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
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.1)
                if sock.connect_ex((self.config.host, int(self.config.port))) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(f"Uvicorn test server failed to start on {self.config.host}:{self.config.port}")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


def _seed_issue_408_timeline(store: SqliteTimelineStore, repo_root: Path) -> None:
    """Populate the smoke-test issue with a realistic coding/review lifecycle."""
    run_dir = repo_root / ".issue-orchestrator" / "sessions" / "flow-run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "terminal-recording.jsonl").write_text(
        '{"event_type":"resize","offset_ms":0,"rows":24,"cols":80}\n',
        encoding="utf-8",
    )
    base = {
        "issue_number": 408,
        "timeline_schema_version": 4,
        "logical_run": 1,
        "logical_cycle": 1,
        "views": ["user", "ops", "debug"],
        "run_id": "flow-run-1",
        "run_dir": str(run_dir),
    }

    records = [
        TimelineRecord(
            event_id="408-session-started",
            timestamp="2026-01-01T12:00:00Z",
            event="session.started",
            source_event="session.started",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Coding session started",
                "summary": "Implement card timeline affordance",
            },
        ),
        TimelineRecord(
            event_id="408-session-completed",
            timestamp="2026-01-01T12:08:00Z",
            event="session.completed",
            source_event="session.completed",
            data={
                **base,
                "logical_phase": "coding",
                "event_intent": "coding",
                "agent": "agent:web",
                "task": "coding",
                "narrative": "Agent finished coding",
                "summary": "Timeline button opens issue detail drawer",
            },
        ),
        TimelineRecord(
            event_id="408-review-started",
            timestamp="2026-01-01T12:10:00Z",
            event="review.started",
            source_event="review.started",
            data={
                **base,
                "logical_phase": "review",
                "event_intent": "review",
                "agent": "agent:reviewer",
                "reviewer_agent": "agent:reviewer",
                "task": "review",
                "narrative": "Code review started",
                "summary": "Reviewer checking timeline affordance",
            },
        ),
        TimelineRecord(
            event_id="408-review-approved",
            timestamp="2026-01-01T12:14:00Z",
            event="review.approved",
            source_event="review.approved",
            data={
                **base,
                "logical_phase": "review",
                "event_intent": "review",
                "agent": "agent:reviewer",
                "reviewer_agent": "agent:reviewer",
                "task": "review",
                "narrative": "Review approved",
                "summary": "Timeline affordance verified",
            },
        ),
    ]
    for record in records:
        store.append(408, record)


def _configure_flow_deps(orchestrator: FlowWebMockOrchestrator, repo_root: Path) -> None:
    state_dir = repo_root / ".issue-orchestrator" / "state"
    state_dir.mkdir(parents=True)
    store = SqliteTimelineStore(db_path=state_dir / "timeline.sqlite")
    _seed_issue_408_timeline(store, repo_root)

    deps = MagicMock()
    deps.timeline_store = store
    deps.timeline_reader = DefaultTimelineReader(store)
    deps.publish_recovery.can_retry_publish.return_value = False
    orchestrator.deps = deps
    orchestrator.config.repo_root = repo_root
    orchestrator.config.config_path = repo_root / ".issue-orchestrator" / "config" / "default.yaml"


@pytest.fixture(scope="module")
def web_server(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the dashboard app with a deterministic mock orchestrator."""
    orchestrator = FlowWebMockOrchestrator()
    repo_root = tmp_path_factory.mktemp("flow-dashboard-repo")
    _configure_flow_deps(orchestrator, repo_root)
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
