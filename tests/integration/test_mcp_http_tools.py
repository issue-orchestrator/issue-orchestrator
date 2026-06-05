"""Integration tests for MCP HTTP adapter tool coverage."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import uvicorn

from issue_orchestrator.entrypoints import web
from issue_orchestrator.execution.orchestrator_http_api import OrchestratorHttpApi
from issue_orchestrator.domain.models import Session, Issue, AgentConfig
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets
from tests.integration.conftest import xdist_timeout
from tests.unit.session_run_helpers import make_session_run_assets


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_server(port: int) -> uvicorn.Server:
    config = uvicorn.Config(web.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + xdist_timeout(5)
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("Uvicorn server failed to start")
    setattr(server, "_thread", thread)
    return server


def _stop_server(server: uvicorn.Server) -> None:
    server.should_exit = True
    thread = getattr(server, "_thread", None)
    if thread:
        thread.join(timeout=5)


def _make_api(base_url: str):
    return OrchestratorHttpApi(lambda: base_url), None


def _write_manifest(worktree: Path, session_name: str, log_path: Path) -> SessionRunAssets:
    run_assets = make_session_run_assets(
        worktree,
        session_name=session_name,
        run_id="20260122-120000Z",
    )
    manifest = json.loads(run_assets.manifest_path.read_text())
    manifest.update(
        {
            "issue_number": 7,
            "claude_log_path": str(log_path),
        }
    )
    run_assets.manifest_path.write_text(json.dumps(manifest))
    index_path = worktree / ".issue-orchestrator" / "sessions" / "index.json"
    index_path.write_text(json.dumps({
        "runs": [{
            "session_name": session_name,
            "run_id": "20260122-120000Z",
            "started_at": run_assets.started_at,
            "issue_number": 7,
            "run_dir": str(run_assets.run_dir),
        }]
    }))
    return run_assets


def _make_session(worktree: Path, run_assets: SessionRunAssets) -> Session:
    issue = Issue(number=7, title="Test", labels=["agent:web"])
    agent_config = AgentConfig(prompt_path=worktree / "prompt.txt", model="sonnet", timeout_minutes=30)
    issue_key = FakeIssueKey(name="7")
    session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=run_assets.session_name,
        worktree_path=worktree,
        branch_name="feature/7",
        run_assets=run_assets,
    )


def test_session_logs_and_phases(sample_orchestrator, tmp_path):
    web.set_orchestrator(sample_orchestrator)
    try:
        worktree = tmp_path / "worktree-7"
        worktree.mkdir()
        log_path = worktree / "claude.jsonl"
        log_path.write_text("{\"type\": \"assistant\", \"content\": \"hello\"}\n")
        run_assets = _write_manifest(worktree, "coding-1", log_path)
        session = _make_session(worktree, run_assets)
        sample_orchestrator.state.active_sessions = [session]

        port = _find_free_port()
        server = _start_server(port)
        api, _ = _make_api(f"http://127.0.0.1:{port}")
        try:
            phases = api.session_phases(7)
            assert phases["issue_number"] == 7
            assert phases["phases"][0]["name"] == "coding-1"

            log = api.session_claude_log(7, 10)
            assert log["entry_count"] == 1
        finally:
            _stop_server(server)
    finally:
        web.set_orchestrator(None)


def test_control_tools_pause_resume_refresh(sample_orchestrator):
    web.set_orchestrator(sample_orchestrator)
    try:
        port = _find_free_port()
        server = _start_server(port)
        api, _ = _make_api(f"http://127.0.0.1:{port}")
        try:
            api.pause()
            assert sample_orchestrator.state.paused is True
            api.resume()
            assert sample_orchestrator.state.paused is False
            api.refresh(["123"])
        finally:
            _stop_server(server)
    finally:
        web.set_orchestrator(None)
