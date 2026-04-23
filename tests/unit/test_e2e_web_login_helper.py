"""Smoke tests for the Playwright login helper in ``tests/e2e_web/conftest.py``.

The helper itself lives in a Playwright-only conftest and never runs
under ``pytest tests/unit/`` without a browser. These tests just
exercise the import + no-auth short-circuit so the helper doesn't
silently rot before the Web Dashboard auth PR arrives.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.e2e_web.conftest import (
    TEST_ADMIN_TOKEN,
    cc_admin_token,
    login_via_form,
)


def test_admin_token_fixture_returns_known_value() -> None:
    fn = cc_admin_token.__wrapped__  # unwrap @pytest.fixture
    assert fn() == TEST_ADMIN_TOKEN


def test_login_via_form_is_no_op_when_dashboard_already_rendered() -> None:
    """If the landing page is already the dashboard, the helper must not
    touch the form — the web dashboard doesn't serve one today.
    """
    page = MagicMock()
    page.content.return_value = (
        '<html><head><meta name="io-csrf-token" content="abc"></head>'
        '<body>Issue Orchestrator</body></html>'
    )

    login_via_form(page, "http://localhost:8080", TEST_ADMIN_TOKEN)

    page.goto.assert_called_once_with("http://localhost:8080/")
    page.fill.assert_not_called()
    page.click.assert_not_called()


def test_login_via_form_submits_form_when_login_page_is_served() -> None:
    page = MagicMock()
    page.content.return_value = (
        "<html><body><form><h1>Sign in</h1>"
        '<input name="token"><button type="submit"></button>'
        "</form></body></html>"
    )

    login_via_form(page, "http://localhost:19080", "some-token")

    page.goto.assert_called_once_with("http://localhost:19080/")
    page.fill.assert_called_once_with('input[name="token"]', "some-token")
    page.click.assert_called_once_with('button[type="submit"]')
    page.wait_for_url.assert_called_once_with(
        "http://localhost:19080/", timeout=5000
    )
