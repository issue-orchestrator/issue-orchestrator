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
    expect(page.locator("#issueDetailTitle")).to_contain_text("Flow smoke item")
    expect(page.locator("#issueDetailDrawer")).to_have_attribute("data-lifecycle-kind", "dashboard")
    expect(page.locator("#issueDetailDrawer")).to_have_attribute("data-lifecycle-iterations", "1")


def test_issue_card_timeline_button_opens_cycle_timeline(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """The visible card Timeline affordance opens the cycle-aware drawer."""
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")

    timeline_btn = page.locator(
        ".dashboard-columns .issue-card[data-issue='408'] .card-timeline-btn"
    ).first
    expect(timeline_btn).to_be_visible(timeout=5000)
    timeline_btn.click()

    drawer = page.locator("#issueDetailDrawer.visible")
    expect(drawer).to_be_visible(timeout=5000)
    expect(page.locator("#issueDetailTitle")).to_contain_text("Flow smoke item")
    expect(page.locator("#issueDetailDrawer")).to_have_attribute("data-lifecycle-kind", "dashboard")

    journey = page.locator("#issueDetailJourney")
    expect(page.locator("#issueDetailTimelineHeading")).to_be_focused()
    expect(journey.locator(".journey-run").first).to_be_visible(timeout=5000)
    expect(journey.locator(".journey-cycle").first).to_be_visible(timeout=5000)
    expect(journey).to_contain_text("Cycle 1")
    expect(journey).to_contain_text("Coding")
    expect(journey).to_contain_text("Review")
    expect(journey).to_contain_text("Agent finished coding")
    expect(journey).to_contain_text("Review approved")
    expect(journey.locator(".timeline-empty")).to_have_count(0)


def test_e2e_tab_navigation_works(page: Page, web_server: dict[str, object]) -> None:
    """Dashboard tabs should navigate to E2E view."""
    page.goto(str(web_server["url"]), wait_until="domcontentloaded")

    page.locator("#tab-e2e").click()
    page.wait_for_url("**?tab=e2e**")
    expect(page.locator("#panel-e2e")).to_be_visible()
