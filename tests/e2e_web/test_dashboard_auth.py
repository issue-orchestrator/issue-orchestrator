"""Browser-level sanity checks for Web Dashboard auth (PR 8).

Unit-level behaviour is pinned in ``tests/unit/test_web_dashboard_auth.py``.
This file exercises the same gate through a real Chromium browser so
we catch regressions in the login-form HTML, the cookie set by the
``303`` redirect, and the "already logged in" short-circuit.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import login_via_form


@pytest.mark.usefixtures("browser_context_args")
def test_dashboard_requires_login(
    page: Page, authed_web_server: dict[str, object]
) -> None:
    """Anonymous browser hitting the dashboard sees the login form."""
    base_url = authed_web_server["url"]
    assert isinstance(base_url, str)
    page.goto(f"{base_url}/")
    expect(page.locator("h1")).to_contain_text("Issue Orchestrator")
    expect(page.locator('input[name="token"]')).to_be_visible()


@pytest.mark.usefixtures("browser_context_args")
def test_login_with_valid_token_lands_on_dashboard(
    page: Page,
    authed_web_server: dict[str, object],
    cc_admin_token: str,
) -> None:
    """The login handler 303s back to ``/``, which then renders the
    dashboard (no more login form)."""
    base_url = authed_web_server["url"]
    assert isinstance(base_url, str)

    login_via_form(page, base_url, cc_admin_token)

    # After login we should see the dashboard shell, not the login
    # form. The dashboard template has a distinct marker.
    page.wait_for_load_state("networkidle")
    assert "Sign in" not in page.content()


@pytest.mark.usefixtures("browser_context_args")
def test_invalid_token_rerenders_form_with_error(
    page: Page, authed_web_server: dict[str, object]
) -> None:
    base_url = authed_web_server["url"]
    assert isinstance(base_url, str)
    page.goto(f"{base_url}/")
    page.fill('input[name="token"]', "not-the-admin-token")
    page.click('button[type="submit"]')
    expect(page.locator(".err")).to_contain_text("Invalid token")


@pytest.mark.usefixtures("browser_context_args")
def test_session_cookie_reaches_mounted_control_route(
    page: Page,
    authed_web_server: dict[str, object],
    cc_admin_token: str,
) -> None:
    """Logged-in dashboard can call ``/control/*`` through the same origin.

    Closes the residual coverage gap from #6041 re-review: the unit
    tests pin the middleware gate synthetically, but only a real
    browser proves that the ``Set-Cookie`` from the ``POST /login``
    303 redirect propagates to ``/control/*`` requests fired from the
    dashboard page.

    One extra fetch via ``page.evaluate`` — no second page load, so
    this adds <1s to the suite.
    """
    base_url = authed_web_server["url"]
    assert isinstance(base_url, str)

    login_via_form(page, base_url, cc_admin_token)

    status = page.evaluate(
        """async () => {
            const r = await fetch('/control/orchestrator/status', {
                credentials: 'same-origin',
            });
            return r.status;
        }"""
    )
    # 401/403 would mean the cookie didn't reach the gate. Anything
    # else (200, 404, 5xx) means the dashboard middleware accepted
    # the cookie on the mounted path.
    assert status not in (401, 403), status
