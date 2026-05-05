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


def test_e2e_watcher_clients_pick_up_env_token(monkeypatch):
    """Pin the watcher auth-wiring contract at its single owner.

    Both watcher call sites (``tests/e2e/flows.py::create_watcher_for_port``
    and ``tests/e2e/conftest.py::orchestrator_watcher``) route through
    ``tests/e2e/_watcher_auth.py::build_watcher_clients``. Testing the
    helper covers both call sites at once — a regression that swaps
    one path back to a file-only helper would still fail this test
    because the helper is the contract.

    Set ``ISSUE_ORCHESTRATOR_API_TOKEN`` and verify all three clients
    (SSE / snapshot / replay) receive that env-token via their
    ``auth_token`` field.
    """
    from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR
    from tests.e2e._watcher_auth import build_watcher_clients

    monkeypatch.setenv(TOKEN_ENV_VAR, "shared-helper-env-token")
    sse, snapshot, replay = build_watcher_clients(19080)

    assert sse.auth_token == "shared-helper-env-token", (
        "SSE client missing env-token; helper or env-precedence regressed"
    )
    assert snapshot.auth_token == "shared-helper-env-token", (
        "snapshot client missing env-token; helper or env-precedence regressed"
    )
    assert replay.auth_token == "shared-helper-env-token", (
        "replay client missing env-token; helper or env-precedence regressed"
    )


def test_e2e_watcher_clients_explicit_token_wins(monkeypatch):
    """Caller-supplied ``auth_token`` overrides the resolved token,
    so tests that need to drive a specific value (e.g. the negative
    "no token" case) don't have to muck with env state.
    """
    from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR
    from tests.e2e._watcher_auth import build_watcher_clients

    monkeypatch.setenv(TOKEN_ENV_VAR, "env-loses")
    sse, snapshot, replay = build_watcher_clients(19080, auth_token="explicit-wins")
    assert sse.auth_token == "explicit-wins"
    assert snapshot.auth_token == "explicit-wins"
    assert replay.auth_token == "explicit-wins"


def test_e2e_watcher_call_sites_route_through_shared_helper():
    """Guardrail: the contract is "both watcher paths route through
    ``build_watcher_clients``". Asserting on the *imports* keeps a
    future PR from accidentally re-introducing the duplicated
    construction blocks (and therefore the auth-wiring drift this
    helper exists to prevent).
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    flows_text = (repo_root / "tests" / "e2e" / "flows.py").read_text()
    conftest_text = (repo_root / "tests" / "e2e" / "conftest.py").read_text()

    for name, text in (("flows.py", flows_text), ("conftest.py", conftest_text)):
        assert "build_watcher_clients" in text, (
            f"{name} no longer references the shared "
            "build_watcher_clients helper; the auth-wiring contract "
            "is now duplicated again."
        )


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
