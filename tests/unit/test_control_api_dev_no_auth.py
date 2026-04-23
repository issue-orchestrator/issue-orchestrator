"""Tests for the --dev-no-auth Control Center mode.

See the dev-mode fix pairing with #6011. When an operator launches
the Control Center with ``--dev-no-auth`` (or the
``ISSUE_ORCHESTRATOR_DEV_NO_AUTH=1`` env var), ``control_center.main``
skips ``configure_api_token`` so the middleware is a no-op. These
tests pin the rendered banner and the banner-gating rule so the
warning cannot be silently removed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from issue_orchestrator.entrypoints.control_api import (
    _DEV_NO_AUTH_BANNER_HTML,
    configure_api_token,
    control_app,
    get_configured_agent_callback_token,
    get_configured_api_token,
)
from issue_orchestrator.infra import browser_session


@pytest.fixture
def auth_disabled_client():
    """TestClient with auth disabled — mirrors --dev-no-auth runtime state."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token(None, agent_callback=None)
    browser_session.initialize(secret=b"test-secret")
    try:
        yield TestClient(control_app)
    finally:
        browser_session.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)


@pytest.fixture
def auth_enabled_client():
    """TestClient with admin token configured — normal production state."""
    prev_admin = get_configured_api_token()
    prev_agent = get_configured_agent_callback_token()
    configure_api_token("test-admin-token", agent_callback="test-agent-token")
    browser_session.initialize(secret=b"test-secret")
    try:
        yield TestClient(control_app)
    finally:
        browser_session.shutdown()
        configure_api_token(prev_admin, agent_callback=prev_agent)


def test_dashboard_renders_dev_banner_when_auth_disabled(
    auth_disabled_client: TestClient,
) -> None:
    resp = auth_disabled_client.get("/")

    assert resp.status_code == 200
    assert "Authentication DISABLED" in resp.text
    assert "--dev-no-auth" in resp.text


def test_dashboard_does_not_render_banner_when_auth_enabled(
    auth_enabled_client: TestClient,
) -> None:
    # Log in first so the dashboard renders instead of the login page.
    login = auth_enabled_client.post(
        "/login",
        headers={"Content-Type": "application/json"},
        json={"token": "test-admin-token"},
    )
    assert login.status_code == 200

    resp = auth_enabled_client.get("/")

    assert resp.status_code == 200
    assert "Authentication DISABLED" not in resp.text
    # Template placeholder must be substituted even in the empty case.
    assert "{{ dev_no_auth_banner }}" not in resp.text


def test_banner_html_is_styled_and_visible() -> None:
    """Pin the banner has the red background + sticky positioning.

    If someone refactors the styling away, the warning becomes a
    tiny gray line people miss. Spot-check the critical style props.
    """
    assert "background:#b91c1c" in _DEV_NO_AUTH_BANNER_HTML
    assert "position:sticky" in _DEV_NO_AUTH_BANNER_HTML
    assert "color:#fff" in _DEV_NO_AUTH_BANNER_HTML


def test_login_page_does_not_show_banner(
    auth_enabled_client: TestClient,
) -> None:
    """The login page is self-contained HTML that does not go through
    the dashboard template, so the banner should never appear there
    even if auth is somehow disabled mid-session.
    """
    resp = auth_enabled_client.get("/")

    assert "Sign in" in resp.text
    assert "Authentication DISABLED" not in resp.text
