"""Integration tests for MCP HTTP adapter and snapshot wiring."""

from __future__ import annotations

from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from issue_orchestrator.entrypoints import mcp_server, web
from issue_orchestrator.execution.orchestrator_http_api import OrchestratorHttpApi
from issue_orchestrator.domain.models import SessionHistoryEntry


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(web.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Uvicorn server failed to start")
    server._thread = thread  # type: ignore[attr-defined]
    return server


def _stop_server(server: uvicorn.Server) -> None:
    server.should_exit = True
    thread = getattr(server, "_thread", None)
    if thread:
        thread.join(timeout=5)


def _make_api(base_url: str):
    client = httpx.Client(base_url=base_url, timeout=5.0)
    return OrchestratorHttpApi(lambda: base_url, client), client


def test_http_api_status_queue_and_history(sample_orchestrator, sample_issues, tmp_path):
    web.set_orchestrator(sample_orchestrator)
    try:
        queue_items = [
            {"number": issue.number, "title": issue.title, "labels": issue.labels}
            for issue in sample_issues
        ]
        sample_orchestrator.state.priority_queue = queue_items
        history_entry = SessionHistoryEntry(
            issue_number=42,
            title="Done",
            agent_type="agent:web",
            status="completed",
            runtime_minutes=12,
            worktree_path=tmp_path / "worktree-42",
        )
        sample_orchestrator.state.session_history = [history_entry]

        port = _find_free_port()
        server = _start_server(port)
        api, client = _make_api(f"http://127.0.0.1:{port}")
        try:
            status = api.status()
            assert "active_sessions" in status
            assert len(status["queue"]) == len(queue_items)

            history = api.history()
            assert history["count"] == 1
            assert history["history"][0]["issue_number"] == 42

            worktree = api.session_worktree(42)
            assert worktree["worktree_path"] == str(history_entry.worktree_path)
        finally:
            client.close()
            _stop_server(server)
    finally:
        web.set_orchestrator(None)


def test_mcp_snapshot_uses_api(sample_orchestrator, sample_issues):
    web.set_orchestrator(sample_orchestrator)
    try:
        queue_items = [
            {"number": issue.number, "title": issue.title, "labels": issue.labels}
            for issue in sample_issues
        ]
        sample_orchestrator.state.priority_queue = queue_items
        port = _find_free_port()
        server = _start_server(port)
        api, client = _make_api(f"http://127.0.0.1:{port}")
        try:
            prev_api = getattr(mcp_server, "_API", None)
            mcp_server._API = api
            snapshot = mcp_server.orchestrator_snapshot()
            assert "status" in snapshot
            assert "info" in snapshot
            assert len(snapshot["status"]["queue"]) == len(queue_items)
        finally:
            if prev_api is not None:
                mcp_server._API = prev_api
            client.close()
            _stop_server(server)
    finally:
        web.set_orchestrator(None)
