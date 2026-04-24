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
