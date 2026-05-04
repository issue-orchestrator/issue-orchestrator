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


def test_e2e_watcher_resolves_token_env_first(monkeypatch):
    """Pin that the e2e watcher uses the env-aware admin-token helper.

    The server resolves the admin token via ``resolve_api_token()`` —
    ``ISSUE_ORCHESTRATOR_API_TOKEN`` wins over the on-disk file. A
    file-only helper on the watcher side would send no token / a
    stale token in env-token configurations and reintroduce the same
    401/timeout shape this PR is meant to fix. Run the helper that
    the watcher call sites use and assert it picks up the env value.
    """
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
