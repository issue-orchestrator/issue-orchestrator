"""Auth middleware tests for the Web Dashboard (port 8080).

Parallels ``test_control_api_auth.py`` — the dashboard uses the same
three-path gate (bearer / session cookie + CSRF / SSE query token)
that the Control Center shipped in #6011. This module pins the gate
against the actual dashboard surface.

See security #5987 F3. The gap this closes: before PR 8 every
dashboard route (``/api/shutdown``, ``/api/open-file``, ``/api/kill``,
the ``/api/events`` SSE stream, and the ``/api/test/*`` fixtures) was
reachable with no credentials.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.web import (
    app,
    configure_dashboard_admin_token,
    get_configured_dashboard_admin_token,
)
from issue_orchestrator.infra import browser_session


@pytest.fixture
def authed_client():
    """TestClient with dashboard auth turned on."""
    prev = get_configured_dashboard_admin_token()
    configure_dashboard_admin_token("test-admin-token")
    try:
        yield TestClient(app)
    finally:
        configure_dashboard_admin_token(prev)


@pytest.fixture
def open_client():
    """TestClient with dashboard auth disabled (test default)."""
    prev = get_configured_dashboard_admin_token()
    configure_dashboard_admin_token(None)
    try:
        yield TestClient(app)
    finally:
        configure_dashboard_admin_token(prev)


# ---------------------------------------------------------------------------
# Bearer token path
# ---------------------------------------------------------------------------


def test_missing_header_on_mutating_route_returns_401(
    authed_client: TestClient,
) -> None:
    # /api/test/cleanup was an unauthenticated destructive endpoint
    # before PR 8 — pin it.
    resp = authed_client.post("/api/test/cleanup")
    assert resp.status_code == 401
    assert "credentials" in resp.json()["error"]


def test_wrong_bearer_token_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/api/test/cleanup",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert resp.json() == {"error": "invalid bearer token"}


def test_correct_bearer_token_passes_middleware(
    authed_client: TestClient,
) -> None:
    """Valid bearer passes auth — the 500 downstream is immaterial
    because the orchestrator isn't wired in this test; what matters
    is that the middleware did not itself return 401.
    """
    resp = authed_client.post(
        "/api/test/cleanup",
        headers={"Authorization": "Bearer test-admin-token"},
    )
    assert resp.status_code != 401


def test_get_view_model_requires_auth(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/view-model")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Public paths
# ---------------------------------------------------------------------------


def test_static_asset_is_public(authed_client: TestClient) -> None:
    # Any request under /static/ should bypass auth. Even a 404 (the
    # asset may not exist in this layout) proves the gate didn't
    # short-circuit.
    resp = authed_client.get("/static/brand/logo.svg")
    assert resp.status_code != 401


def test_favicon_is_public(authed_client: TestClient) -> None:
    resp = authed_client.get("/favicon.ico")
    assert resp.status_code == 200


def test_root_renders_login_form_when_unauthenticated(
    authed_client: TestClient,
) -> None:
    """Anonymous visitor to ``/`` gets the login form, not the raw
    dashboard HTML or a 401 JSON blob. Parallels the Control Center.
    """
    resp = authed_client.get("/")
    assert resp.status_code == 200
    assert "Sign in" in resp.text
    assert 'action="/login"' in resp.text


# ---------------------------------------------------------------------------
# Login flow + session cookie
# ---------------------------------------------------------------------------


def test_login_with_wrong_token_returns_form_with_error(
    authed_client: TestClient,
) -> None:
    resp = authed_client.post(
        "/login",
        data={"token": "nope"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    assert "Invalid token" in resp.text


def test_login_with_wrong_token_json_returns_401(
    authed_client: TestClient,
) -> None:
    resp = authed_client.post(
        "/login", json={"token": "nope"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid token"


def test_login_mints_session_cookie(authed_client: TestClient) -> None:
    browser_session.initialize()
    resp = authed_client.post(
        "/login", json={"token": "test-admin-token"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["session_id"]
    assert browser_session.SESSION_COOKIE in resp.cookies


def test_mutating_request_with_session_requires_csrf(
    authed_client: TestClient,
) -> None:
    """Logged-in browser POSTs must carry ``X-CSRF-Token`` — defending
    against cross-site request forgery from another tab.
    """
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200

    # Same client keeps the cookie; omit X-CSRF-Token.
    resp = authed_client.post("/api/test/cleanup")
    assert resp.status_code == 403
    assert "csrf" in resp.json()["error"].lower()


def test_safe_method_with_session_does_not_need_csrf(
    authed_client: TestClient,
) -> None:
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200
    # GET should succeed purely on the session cookie.
    resp = authed_client.get("/api/view-model")
    assert resp.status_code != 401
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# SSE token
# ---------------------------------------------------------------------------


def test_sse_without_session_returns_401(authed_client: TestClient) -> None:
    resp = authed_client.get("/api/events")
    assert resp.status_code == 401


def test_sse_token_is_accepted_by_middleware(
    authed_client: TestClient,
) -> None:
    """Logged-in browser flow: POST /login → GET /api/sse-token.

    We can't exercise ``/api/events`` itself in a TestClient unit test
    (the SSE handler streams forever), but the middleware is the
    gate: if ``evaluate_request`` accepts the query-string token the
    request is through. Assert the gate decision directly here and
    leave the end-to-end reconnect path to the e2e suite.
    """
    from issue_orchestrator.entrypoints._auth_middleware import (
        check_browser_session_auth,
    )
    from issue_orchestrator.entrypoints.web import _DASHBOARD_SURFACE

    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200

    token_resp = authed_client.get("/api/sse-token")
    assert token_resp.status_code == 200
    token = token_resp.json()["sse_token"]
    assert token

    # Build a bare Request that mirrors what the SSE client would send.
    class _Req:
        def __init__(self, session_id: str, token: str) -> None:
            self.cookies = {browser_session.SESSION_COOKIE: session_id}
            self.query_params = {browser_session.SSE_TOKEN_QUERY: token}
            self.headers: dict[str, str] = {}
            self.method = "GET"

            class _URL:
                path = _DASHBOARD_SURFACE.sse_path

            self.url = _URL()

    # Grab the session id from the TestClient's cookie jar.
    session_id = authed_client.cookies.get(browser_session.SESSION_COOKIE)
    assert session_id

    ok, message, status = check_browser_session_auth(
        _Req(session_id, token),  # type: ignore[arg-type]
        _DASHBOARD_SURFACE,
    )
    assert ok, (message, status)


def test_sse_with_wrong_token_is_rejected(
    authed_client: TestClient,
) -> None:
    browser_session.initialize()
    login = authed_client.post("/login", json={"token": "test-admin-token"})
    assert login.status_code == 200
    resp = authed_client.get("/api/events?sse_token=obviously-wrong")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Off-by-default behaviour
# ---------------------------------------------------------------------------


def test_unconfigured_admin_token_means_no_gate(
    open_client: TestClient,
) -> None:
    """TestClient default (no admin token) leaves every route open,
    matching the existing behaviour unit tests rely on.
    """
    # /api/view-model is a read route; if auth were somehow enforced
    # we'd see 401. We only care that the middleware doesn't block
    # it.
    resp = open_client.get("/api/view-model")
    assert resp.status_code != 401
