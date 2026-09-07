"""Tests for agent-triggered orchestrator resume callbacks."""

from __future__ import annotations

from issue_orchestrator.domain.completion_intake import CompletionIntakeReceipt

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

    def read(self, size: int) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def test_trigger_orchestrator_resume_posts_exact_receipt_with_capability(
    monkeypatch,
) -> None:
    captured: dict[str, urllib.request.Request] = {}

    def _urlopen(request: urllib.request.Request, timeout: int) -> _Response:
        assert timeout == 120
        captured["request"] = request
        return _Response({"success": True})

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "12345")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "42")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY", "test-capability")
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: type("Transport", (), {"open": staticmethod(_urlopen)})(),
    )

    success, error = trigger_orchestrator_resume(
        receipt=CompletionIntakeReceipt("a" * 64, "b" * 64)
    )

    assert success is True
    assert error is None
    request = captured["request"]
    assert request.full_url == "http://127.0.0.1:12345/api/issues/42/resume"
    assert request.data is not None
    assert json.loads(request.data.decode("utf-8")) == {
        "entry_id": "a" * 64,
        "content_sha256": "b" * 64,
    }


def test_trigger_orchestrator_resume_requires_capability_before_fetch(
    monkeypatch,
) -> None:
    called = False

    def _urlopen(*_args: object, **_kwargs: object) -> _Response:
        nonlocal called
        called = True
        return _Response({"success": True})

    monkeypatch.setenv("ISSUE_ORCHESTRATOR_API_PORT", "12345")
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_ISSUE_NUMBER", "42")
    monkeypatch.delenv("ISSUE_ORCHESTRATOR_COMPLETION_CAPABILITY", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *handlers: type("Transport", (), {"open": staticmethod(_urlopen)})(),
    )

    success, error = trigger_orchestrator_resume(
        receipt=CompletionIntakeReceipt("a" * 64, "b" * 64)
    )

    assert success is False
    assert error is not None
    assert "completion intake capability required" in error
    assert called is False
