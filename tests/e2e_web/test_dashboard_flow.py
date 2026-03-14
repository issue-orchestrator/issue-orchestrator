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


def test_provider_outage_banner_hidden_when_no_outage(page: Page, web_server: dict[str, object]) -> None:
    """Provider outage banner should be hidden when no circuits are open."""
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.wait_for_timeout(300)

    banner = page.locator("#providerOutageBanner")
    expect(banner).to_be_hidden()
    assert errors == []


def test_provider_outage_banner_shown_via_sse(page: Page, web_server: dict[str, object]) -> None:
    """Provider outage banner appears when provider.outage_entered SSE event is received."""
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.wait_for_timeout(300)

    # Simulate the SSE event via JS evaluation
    from datetime import datetime, timedelta, timezone
    open_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    page.evaluate(f"""
        onProviderOutageEntered({{
            provider: 'claude',
            open_until: '{open_until}',
            consecutive_outages: 1,
            error_summary: 'Test outage'
        }});
    """)

    banner = page.locator("#providerOutageBanner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("claude")
    expect(banner).to_contain_text("Provider outage")
    assert errors == []


def test_provider_outage_banner_clears_on_exit(page: Page, web_server: dict[str, object]) -> None:
    """Provider outage banner hides when provider.outage_exited SSE event clears it."""
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.wait_for_timeout(300)

    from datetime import datetime, timedelta, timezone
    open_until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    page.evaluate(f"""
        onProviderOutageEntered({{
            provider: 'claude',
            open_until: '{open_until}',
            consecutive_outages: 1,
            error_summary: null
        }});
    """)
    expect(page.locator("#providerOutageBanner")).to_be_visible()

    page.evaluate("onProviderOutageExited({ provider: 'claude', at: new Date().toISOString() });")
    expect(page.locator("#providerOutageBanner")).to_be_hidden()
