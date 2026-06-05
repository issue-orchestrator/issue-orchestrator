"""Tests for agent-triggered orchestrator resume callbacks."""

from __future__ import annotations

import json
import urllib.request

from issue_orchestrator.entrypoints.cli_tools.orchestrator_resume import (
    trigger_orchestrator_resume,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_trigger_orchestrator_resume_posts_owner_injected_run_dir(
    monkeypatch,
) -> None:
    captured: dict[str, urllib.request.Request] = {}

    def _urlopen(request: urllib.request.Request, timeout: int) -> _Response:
        assert timeout == 120
        captured["request"] = request
        return _Response({"success": True})

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "12345")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "42")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_RUN_DIR", "/tmp/io-run")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    success, error = trigger_orchestrator_resume()

    assert success is True
    assert error is None
    request = captured["request"]
    assert request.full_url == "http://localhost:12345/api/issues/42/resume"
    assert request.data is not None
    assert json.loads(request.data.decode("utf-8")) == {"run_dir": "/tmp/io-run"}


def test_trigger_orchestrator_resume_requires_run_dir_before_fetch(
    monkeypatch,
) -> None:
    called = False

    def _urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal called
        called = True
        return _Response({"success": True})

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "12345")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "42")
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_RUN_DIR", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)

    success, error = trigger_orchestrator_resume()

    assert success is False
    assert error is not None
    assert "ISSUE_ORCHESTRATOR_RUN_DIR is required" in error
    assert called is False
