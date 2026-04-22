"""Bearer-token middleware tests for the Control API.

See security issue #5987 (F3). When ``configure_api_token`` has been
called, every HTTP request to the Control API must carry an
``Authorization: Bearer <token>`` header that matches the configured
secret. When unconfigured (the default in unit tests), the middleware
is a no-op so existing TestClient fixtures keep working.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    configure_api_token,
    control_app,
    get_configured_api_token,
)


@pytest.fixture
def authed_client():
    """TestClient wrapped around control_app with a known bearer token enabled."""
    previous = get_configured_api_token()
    configure_api_token("test-token-value")
    try:
        yield TestClient(control_app)
    finally:
        configure_api_token(previous)


def test_missing_header_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/status")

    assert resp.status_code == 401
    assert resp.json() == {"error": "missing bearer token"}


def test_wrong_scheme_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Basic dXNlcjpwYXNz"}
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "missing bearer token"}


def test_wrong_token_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Bearer wrong-token"}
    )

    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid bearer token"}


def test_correct_token_passes_middleware(authed_client: TestClient) -> None:
    # /api/status requires an orchestrator to be set to return a 200; the
    # point here is that middleware does NOT reject it. Anything other
    # than 401 proves the token check passed.
    resp = authed_client.get(
        "/api/status", headers={"Authorization": "Bearer test-token-value"}
    )

    assert resp.status_code != 401


def test_no_token_configured_means_no_enforcement() -> None:
    """Default unit-test setup: auth is off and requests flow through."""
    previous = get_configured_api_token()
    configure_api_token(None)
    try:
        client = TestClient(control_app)
        resp = client.get("/api/status")
        assert resp.status_code != 401
    finally:
        configure_api_token(previous)


def test_mutating_route_also_requires_token(authed_client: TestClient) -> None:
    """POST routes must be gated, not just GETs."""
    resp = authed_client.post("/api/pause")

    assert resp.status_code == 401


def test_sse_route_also_requires_token(authed_client: TestClient) -> None:
    """The SSE event stream leaks internal state; it must require auth too."""
    resp = authed_client.get("/api/events")

    assert resp.status_code == 401


def test_bearer_token_comparison_is_constant_time() -> None:
    """verify_token must accept the exact token and reject anything else.

    Actual timing-attack resistance is covered in test_api_token.py; this
    test pins the behavioural contract that the middleware relies on.
    """
    from issue_orchestrator.infra.api_token import verify_token

    assert verify_token("abc", "abc")
    assert not verify_token("abc", "abd")
    assert not verify_token("abc", "abcd")
