"""Flow-first browser smoke tests for dashboard runtime behavior."""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_dashboard_loads_without_page_errors(page: Page, web_server: dict[str, object]) -> None:
    """Dashboard JS should execute without runtime exceptions on initial load."""
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.wait_for_timeout(500)

    expect(page.locator("#tab-dashboard")).to_be_visible()
    assert errors == []


def test_flow_card_opens_issue_detail_drawer(page: Page, web_server: dict[str, object]) -> None:
    """Clicking a flow card focus button opens the issue detail drawer."""
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")

    card_focus = page.locator(".dashboard-columns .issue-card[data-issue='408'] .card-focus").first
    expect(card_focus).to_be_visible()
    card_focus.click()

    expect(page.locator("#issueDetailDrawer.visible")).to_be_visible()
    expect(page.locator("#issueDetailTitle")).to_contain_text("Issue #408")


def test_e2e_tab_navigation_works(page: Page, web_server: dict[str, object]) -> None:
    """Dashboard tabs should navigate to E2E view."""
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")

    page.locator("#tab-e2e").click()
    page.wait_for_url("**?tab=e2e**")
    expect(page.locator("#panel-e2e")).to_be_visible()
