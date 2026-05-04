"""Unit coverage for SSE/HTTP test client auth wiring.

The orchestrator's loopback API guards ``/api/events``,
``/api/snapshot``, and ``/api/events_since`` behind
``Authorization: Bearer <token>``. The test client in
``issue_orchestrator.testing.asyncdsl.http`` historically called
``urllib.request.urlopen(url, …)`` with no headers — when used
against an authed endpoint, that returned HTTP 401, the watcher
silently treated the orchestrator as unreachable, and the calling
e2e test failed with a misleading timeout instead of an auth error.

These tests pin that ``auth_token`` is forwarded to every outbound
request from each of the three classes.
"""

from __future__ import annotations

import threading
import urllib.request

import pytest

from issue_orchestrator.testing.asyncdsl.http import (
    HTTPReplayProvider,
    HTTPSnapshotProvider,
    SSEEventStream,
    _build_request,
)


@pytest.mark.asyncio
async def test_e2e_watcher_wires_env_token_through_to_clients(monkeypatch):
    """Pin the watcher *wiring*, not the underlying helper.

    The bug ``read_existing_admin_token`` was introduced to fix is that
    the watcher call sites picked the wrong token source. Asserting
    only the helper would miss a regression where ``flows.py`` /
    ``conftest.py`` switch back to a file-only helper; the helper
    itself would still be correct in isolation.

    Strategy: monkeypatch the three client constructors to capture the
    ``auth_token`` they receive, then call ``create_watcher_for_port``.
    The wiring is verified by what each constructor was asked to use,
    independent of whether the watcher's internal materializer
    succeeds against the canned response.
    """
    from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR
    from issue_orchestrator.testing.asyncdsl import http as asyncdsl_http
    from tests.e2e import flows as e2e_flows

    monkeypatch.setenv(TOKEN_ENV_VAR, "wire-test-env-token")

    constructed: dict[str, str | None] = {}

    class _RecordingSSE(asyncdsl_http.SSEEventStream):
        def __post_init__(self) -> None:
            constructed["sse_token"] = self.auth_token
            super().__post_init__()

        async def start(self) -> None:
            return  # don't spawn a thread; we already captured the token

    class _RecordingSnapshot(asyncdsl_http.HTTPSnapshotProvider):
        # Subclasses without ``@dataclass`` do not inherit the parent's
        # ``__post_init__`` hook, and the parent has none anyway. Override
        # ``__init__`` directly to capture the token.
        def __init__(self, url, auth_token=None):
            super().__init__(url=url, auth_token=auth_token)
            constructed["snapshot_token"] = auth_token

    class _RecordingReplay(asyncdsl_http.HTTPReplayProvider):
        def __init__(self, url, auth_token=None):
            super().__init__(url=url, auth_token=auth_token)
            constructed["replay_token"] = auth_token

    # Patch the names ``flows.py`` imports from so the watcher path
    # picks up the recording subclasses.
    monkeypatch.setattr(e2e_flows, "SSEEventStream", _RecordingSSE)
    monkeypatch.setattr(e2e_flows, "HTTPSnapshotProvider", _RecordingSnapshot)
    monkeypatch.setattr(e2e_flows, "HTTPReplayProvider", _RecordingReplay)

    # ``OrchestratorWatcher.create`` expects a real snapshot stream;
    # short-circuit so the test focuses on the wiring stage.
    class _NoopWatcher:
        @classmethod
        async def create(cls, **_kwargs):
            return cls()

        async def close(self):
            return

    monkeypatch.setattr(e2e_flows, "OrchestratorWatcher", _NoopWatcher)

    watcher, _stream = await e2e_flows.create_watcher_for_port(19080)
    try:
        assert constructed.get("sse_token") == "wire-test-env-token", (
            "SSE client missing env-token; watcher wired wrong helper"
        )
        assert constructed.get("snapshot_token") == "wire-test-env-token", (
            "snapshot client missing env-token; watcher wired wrong helper"
        )
        assert constructed.get("replay_token") == "wire-test-env-token", (
            "replay client missing env-token; watcher wired wrong helper"
        )
    finally:
        await watcher.close()


def test_e2e_watcher_resolves_token_env_first(monkeypatch):
    """Direct sanity check on the env-aware helper used by the watcher
    paths. Paired with the wiring test above so a regression in either
    the helper or its wiring fails its own test."""
    from issue_orchestrator.infra.api_token import (
        TOKEN_ENV_VAR,
        read_existing_admin_token,
    )

    monkeypatch.setenv(TOKEN_ENV_VAR, "env-token-wins")
    assert read_existing_admin_token() == "env-token-wins"


def test_build_request_attaches_bearer_token():
    request = _build_request("http://localhost:19080/api/events", auth_token="abc123")
    assert request.get_header("Authorization") == "Bearer abc123"


def test_build_request_omits_authorization_header_when_token_is_none():
    request = _build_request("http://localhost:19080/api/events", auth_token=None)
    assert request.get_header("Authorization") is None


def test_build_request_omits_authorization_header_when_token_is_empty():
    request = _build_request("http://localhost:19080/api/events", auth_token="")
    assert request.get_header("Authorization") is None


class _CapturingResponse:
    """Minimal response stand-in; iter yields nothing so the SSE loop exits cleanly."""

    def __iter__(self):
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"events": []}'


@pytest.mark.asyncio
async def test_snapshot_provider_attaches_bearer_token(monkeypatch):
    captured: dict[str, urllib.request.Request] = {}

    def _fake_urlopen(request, timeout):  # noqa: ARG001
        captured["request"] = request
        return _CapturingResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = HTTPSnapshotProvider(
        "http://localhost:19080/api/snapshot", auth_token="snap-token",
    )
    await provider.fetch_snapshot()
    assert captured["request"].get_header("Authorization") == "Bearer snap-token"


@pytest.mark.asyncio
async def test_replay_provider_attaches_bearer_token(monkeypatch):
    captured: dict[str, urllib.request.Request] = {}

    def _fake_urlopen(request, timeout):  # noqa: ARG001
        captured["request"] = request
        return _CapturingResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = HTTPReplayProvider(
        "http://localhost:19080/api/events_since", auth_token="replay-token",
    )
    await provider.fetch_events_since(0)
    request = captured["request"]
    assert request.get_header("Authorization") == "Bearer replay-token"
    # The replay path also tacks ``?after=<id>`` onto the URL — make sure
    # the auth header survives that string concatenation.
    assert request.get_full_url().endswith("?after=0")


@pytest.mark.asyncio
async def test_sse_event_stream_attaches_bearer_token(monkeypatch):
    """Drives the SSE stream through its public ``start``/``close`` API.

    The fake ``urlopen`` records the request and returns a response
    with no lines so ``_process_stream`` exits immediately; the test
    closes the stream right after to end the reconnect loop.
    """
    import asyncio

    captured: dict[str, urllib.request.Request] = {}
    captured_event = threading.Event()

    def _fake_urlopen(request, timeout):  # noqa: ARG001
        captured["request"] = request
        captured_event.set()
        return _CapturingResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    stream = SSEEventStream(
        "http://localhost:19080/api/events",
        auth_token="sse-token",
        timeout_s=0.1,
    )
    await stream.start()
    try:
        # Wait briefly for the worker thread to issue its first request.
        await asyncio.to_thread(captured_event.wait, 2.0)
    finally:
        await stream.close()
    assert "request" in captured, "SSE worker never called urlopen"
    assert captured["request"].get_header("Authorization") == "Bearer sse-token"
