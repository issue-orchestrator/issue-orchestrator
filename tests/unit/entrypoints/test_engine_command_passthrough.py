"""Control Center -> Repository Engine command passthrough, both sides (#8222).

The bug: a Control Center pause or resume answered
``{"error": "passthrough_failed", "detail": ""}``. The engine applies those
commands under its state lock, which a running tick holds for the whole tick
(32s on the day of the report). The engine's async route waited for that lock
ON its event loop, freezing every route; the Control Center's 10s read timeout
expired; and ``str(httpx.ReadTimeout())`` is ``""``.

These tests run the real engine app (``entrypoints.web.app``, which also
mounts ``control_app``) under uvicorn on a loopback port, with an engine
stand-in that owns a real ``PauseController`` behind a real state lock and the
real pause facade, and drive it through the real Control Center command
objects over real HTTP.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import uvicorn

from issue_orchestrator.control import pause_facade
from issue_orchestrator.control.pause_controller import PauseController
from issue_orchestrator.domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseState,
    PauseTransitionOutcome,
)
from issue_orchestrator.events.context import EventContext
from issue_orchestrator.execution.control_center_actions import (
    EngineCommandForwarder,
    PauseOrchestratorCommand,
    RefreshActionRequest,
    RefreshOrchestratorCommand,
    RepoActionRequest,
    ResumeOrchestratorCommand,
)
from issue_orchestrator.execution.engine_command_failure import (
    ENGINE_COMMAND_TIMEOUT_SECONDS,
)
from issue_orchestrator.execution.orchestrator_http_api import post_orchestrator_json
from issue_orchestrator.infra.supervisor import SupervisorStatus

REPO = Path("/tmp/repo-8222")


class _TickingEngine:
    """The Orchestrator facade's command surface, with a tick you can hold open.

    ``pause``/``resume`` go through the production pause facade, so they wait
    for the state lock exactly as the real engine does. Every command records
    whether it was invoked on a thread running an event loop — the engine must
    never wait for the state lock there.
    """

    def __init__(self) -> None:
        self.state_lock = threading.RLock()
        self.state = SimpleNamespace(
            pause_state=PauseState.running(),
            queue_refresh_in_progress=False,
            active_sessions=[],
        )
        self.pause_controller = PauseController(
            events=MagicMock(), event_context=EventContext(), store=self.state
        )
        self.waited_on_event_loop: list[str] = []
        self.refreshes: list[set[str] | None] = []
        self.shutdowns: list[bool] = []

    def _note_caller(self, command: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self.waited_on_event_loop.append(command)

    def pause(
        self, *, reason: PauseReason, actor: PauseActor, detail: str = ""
    ) -> PauseTransitionOutcome:
        self._note_caller("pause")
        return pause_facade.pause(
            self.pause_controller, self.state_lock, reason=reason, actor=actor, detail=detail
        )

    def resume(self, *, actor: PauseActor, detail: str = "") -> PauseTransitionOutcome:
        self._note_caller("resume")
        return pause_facade.resume(
            self.pause_controller, self.state_lock, actor=actor, detail=detail
        )

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        self._note_caller("refresh")
        with self.state_lock:
            self.refreshes.append(inflight_stable_ids)

    def request_shutdown(self, force: bool = False) -> None:
        self._note_caller("shutdown")
        with self.state_lock:
            self.shutdowns.append(force)

    @contextmanager
    def tick_in_progress(self) -> Iterator[threading.Event]:
        """Hold the state lock on a tick thread until the yielded event is set."""
        holding = threading.Event()
        finish = threading.Event()

        def tick() -> None:
            with self.state_lock:
                holding.set()
                finish.wait(timeout=30)

        thread = threading.Thread(target=tick, name="tick", daemon=True)
        thread.start()
        assert holding.wait(timeout=5)
        try:
            yield finish
        finally:
            finish.set()
            thread.join(timeout=5)


@dataclass
class _LiveEngine:
    engine: _TickingEngine
    port: int
    supervisor: MagicMock = field(default_factory=MagicMock)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def forwarder(self, timeout_seconds: float = 10.0) -> EngineCommandForwarder:
        return EngineCommandForwarder(self.supervisor, timeout_seconds=timeout_seconds)


@pytest.fixture
def live_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[_LiveEngine]:
    from issue_orchestrator.entrypoints import control_api, web

    engine = _TickingEngine()
    # The engine's routes resolve the orchestrator from these module globals.
    monkeypatch.setattr(web, "_orchestrator", engine)
    monkeypatch.setattr(control_api, "_orchestrator", engine)
    # No credentials in play: the engine under test enforces none, and the
    # Control Center must not read the operator's real admin token file.
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_API_TOKEN", raising=False)
    monkeypatch.setattr(
        "issue_orchestrator.infra.api_token.read_existing_admin_token", lambda: None
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(web.app, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="engine-http", daemon=True
    )
    # The shutdown route must not stop this process or leak shutdown state.
    with patch.object(web.shutdown_manager, "exit"), patch.object(
        web.shutdown_manager, "request_shutdown"
    ), patch.object(web, "_server", None):
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "engine server did not start"
            time.sleep(0.02)
        live = _LiveEngine(engine=engine, port=port)
        live.supervisor.status.return_value = SupervisorStatus(state="running", port=port)
        try:
            yield live
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            sock.close()


def _answer_while_engine_waits(
    live: _LiveEngine, send: Any
) -> tuple[Any, float]:
    """Run ``send`` while a tick holds the state lock; release it after 0.5s.

    Returns ``send``'s result and how long it took.
    """
    with live.engine.tick_in_progress() as finish:
        threading.Timer(0.5, finish.set).start()
        started = time.monotonic()
        result = send()
        return result, time.monotonic() - started


# --- The reported failure: CC pause/resume while a tick holds the lock -------


def test_cc_pause_through_a_running_tick_commits_and_reports_the_engines_answer(
    live_engine: _LiveEngine,
) -> None:
    command = PauseOrchestratorCommand(live_engine.forwarder())

    result, _ = _answer_while_engine_waits(
        live_engine,
        lambda: asyncio.run(command.execute(RepoActionRequest(repo_root=REPO))),
    )

    assert result.status_code == 200, result.payload
    assert result.payload["status"] == "paused"
    assert result.payload["committed"] is True
    assert result.payload["actor"] == "control_center"
    assert live_engine.engine.pause_controller.paused is True


def test_cc_resume_through_a_running_tick_commits(live_engine: _LiveEngine) -> None:
    live_engine.engine.pause(reason=PauseReason.OPERATOR, actor=PauseActor.CONTROL_CENTER)
    command = ResumeOrchestratorCommand(live_engine.forwarder())

    result, _ = _answer_while_engine_waits(
        live_engine,
        lambda: asyncio.run(command.execute(RepoActionRequest(repo_root=REPO))),
    )

    assert result.status_code == 200, result.payload
    assert result.payload["status"] == "resumed"
    assert result.payload["committed"] is True
    assert live_engine.engine.pause_controller.paused is False


def test_cc_refresh_through_a_running_tick_reaches_the_engine(
    live_engine: _LiveEngine,
) -> None:
    command = RefreshOrchestratorCommand(live_engine.forwarder())

    result, _ = _answer_while_engine_waits(
        live_engine,
        lambda: asyncio.run(
            command.execute(RefreshActionRequest(repo_root=REPO, inflight_stable_ids=["I_1"]))
        ),
    )

    assert result.status_code == 200, result.payload
    assert result.payload["status"] == "refresh_requested"
    assert live_engine.engine.refreshes == [{"I_1"}]


# --- Engine side: commands never wait for the state lock on the event loop ---


@pytest.mark.parametrize(
    ("path", "body", "command"),
    [
        ("/api/pause", {"actor": "control_center"}, "pause"),
        ("/api/resume", {"actor": "control_center"}, "resume"),
        ("/api/refresh", {"inflight_stable_ids": ["I_1"]}, "refresh"),
        ("/api/shutdown", {"reason": "test #8222", "actor": "unit-test"}, "shutdown"),
    ],
)
def test_engine_keeps_serving_while_a_command_waits_for_the_tick(
    live_engine: _LiveEngine, path: str, body: dict[str, Any], command: str
) -> None:
    """While one command waits out a tick, the engine still answers others.

    Before #8222 the wait happened on the engine's event loop, so a second
    request could not even be read until the tick ended.
    """
    if command == "resume":
        live_engine.engine.pause(reason=PauseReason.OPERATOR, actor=PauseActor.CLI)
    answers: dict[str, httpx.Response] = {}

    with live_engine.engine.tick_in_progress() as finish:
        waiting = threading.Thread(
            target=lambda: answers.setdefault(
                "command", httpx.post(f"{live_engine.base_url}{path}", json=body, timeout=10)
            ),
            daemon=True,
        )
        waiting.start()
        time.sleep(0.3)  # let the command reach the lock wait
        # A second command for the same lock is accepted and waits as well; the
        # probe is a request the engine answers without the lock.
        probe = httpx.get(f"{live_engine.base_url}/api/no-such-route-8222", timeout=2)
        assert probe.status_code == 404  # answered: the loop is free
        assert waiting.is_alive(), "the command must still be waiting for the tick"
        finish.set()
        waiting.join(timeout=10)

    assert answers["command"].status_code == 200, answers["command"].text
    assert live_engine.engine.waited_on_event_loop == []


@pytest.mark.parametrize("path", ["/api/pause", "/api/resume", "/api/refresh"])
def test_control_api_routes_also_wait_off_the_event_loop(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``control_app`` serves the same commands when it runs as its own server."""
    from fastapi.testclient import TestClient

    from issue_orchestrator.entrypoints import control_api

    engine = _TickingEngine()
    monkeypatch.setattr(control_api, "_orchestrator", engine)
    with TestClient(control_api.control_app) as client:
        response = client.post(path, json={})

    assert response.status_code == 200, response.text
    assert engine.waited_on_event_loop == []


# --- Control Center side: every failure names its cause -----------------------


def test_cc_timeout_names_the_wait_and_the_command_still_commits(
    live_engine: _LiveEngine,
) -> None:
    command = PauseOrchestratorCommand(live_engine.forwarder(timeout_seconds=0.5))

    with live_engine.engine.tick_in_progress():
        result = asyncio.run(command.execute(RepoActionRequest(repo_root=REPO)))

    assert result.status_code == 504
    assert result.payload["error"] == "passthrough_failed"
    assert result.payload["failure"] == "no_answer"
    assert result.payload["command"] == "pause"
    detail = result.payload["detail"]
    assert "0.5s" in detail and "ReadTimeout" in detail and live_engine.base_url in detail
    # The detail promises the command may still take effect: it must.
    deadline = time.monotonic() + 5
    while not live_engine.engine.pause_controller.paused:
        assert time.monotonic() < deadline, "the timed-out pause never committed"
        time.sleep(0.02)


def test_cc_reports_the_engines_refusal_with_status_and_body(
    live_engine: _LiveEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from issue_orchestrator.entrypoints import web

    monkeypatch.setattr(web, "_orchestrator", None)  # the engine answers 503
    command = ResumeOrchestratorCommand(live_engine.forwarder())

    result = asyncio.run(command.execute(RepoActionRequest(repo_root=REPO)))

    assert result.status_code == 502
    assert result.payload["failure"] == "upstream_error"
    assert result.payload["upstream_status"] == 503
    assert "Orchestrator not running" in result.payload["upstream_body"]
    assert "503" in result.payload["detail"]
    assert "Orchestrator not running" in result.payload["detail"]


def test_cc_reports_an_unreachable_engine() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()  # nothing listens here
    supervisor = MagicMock()
    supervisor.status.return_value = SupervisorStatus(state="running", port=dead_port)

    result = asyncio.run(
        PauseOrchestratorCommand(EngineCommandForwarder(supervisor)).execute(
            RepoActionRequest(repo_root=REPO)
        )
    )

    assert result.status_code == 502
    assert result.payload["failure"] == "unreachable"
    assert f"127.0.0.1:{dead_port}" in result.payload["detail"]
    assert "ConnectError" in result.payload["detail"]


def test_cc_forwards_with_the_engine_command_budget() -> None:
    """The default budget covers the tick wait, not just network latency."""
    assert EngineCommandForwarder(MagicMock()).timeout_seconds == ENGINE_COMMAND_TIMEOUT_SECONDS
    assert ENGINE_COMMAND_TIMEOUT_SECONDS >= 120


def test_tech_lead_command_transport_names_its_cause(live_engine: _LiveEngine) -> None:
    """The other CC->engine command path reports failures the same way."""
    with live_engine.engine.tick_in_progress():
        answer = post_orchestrator_json(
            f"{live_engine.base_url}/api/pause",
            {"actor": "control_center"},
            command="pause",
            timeout_seconds=0.3,
        )

    assert not isinstance(answer, tuple)
    assert answer.kind == "no_answer"
    assert "ReadTimeout" in answer.detail and "0.3s" in answer.detail
