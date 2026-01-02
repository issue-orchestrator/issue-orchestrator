"""Playwright e2e tests for the web dashboard."""

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def page(browser, web_server) -> Page:
    """Create a new page and navigate to the dashboard."""
    page = browser.new_page()
    page.goto(web_server["url"])
    # Wait for dashboard to be ready
    page.wait_for_selector("header h1")
    yield page
    page.close()


class TestDashboardRenders:
    """Tests for basic dashboard rendering."""

    def test_dashboard_renders_title(self, page: Page):
        """Page loads with Issue Orchestrator title."""
        expect(page.locator("header h1")).to_have_text("Issue Orchestrator")

    def test_dashboard_shows_running_badge(self, page: Page):
        """Status badge shows Running when not paused."""
        badge = page.locator(".status-badge.status-running")
        expect(badge).to_be_visible()
        expect(badge).to_have_text("Running")

    def test_dashboard_shows_paused_badge(self, page: Page, web_server):
        """Status badge shows Paused when orchestrator is paused."""
        web_server["orchestrator"].state.paused = True
        page.reload()
        badge = page.locator(".status-badge.status-paused").first
        expect(badge).to_be_visible()
        expect(badge).to_contain_text("Paused")

    def test_dashboard_shows_repo_in_footer(self, page: Page):
        """Footer displays repository name."""
        expect(page.locator(".footer")).to_contain_text("test/repo")

    def test_dashboard_empty_state(self, page: Page):
        """Shows empty state message when no issues."""
        expect(page.locator(".empty-state")).to_be_visible()


class TestIssueDisplay:
    """Tests for issue row display."""

    def test_active_session_displays(self, page: Page, web_server):
        """Dashboard displays active sessions."""
        web_server["orchestrator"].add_active_session(123, "Test Active Issue")
        page.reload()
        expect(page.locator(".issue-row")).to_be_visible()
        expect(page.locator(".issue-num")).to_contain_text("#123")
        expect(page.locator(".issue-title")).to_contain_text("Test Active Issue")

    def test_queue_issue_displays(self, page: Page, web_server):
        """Dashboard displays queued issues."""
        web_server["orchestrator"].add_queue_issue(456, "Queued Issue")
        page.reload()
        expect(page.locator(".issue-row")).to_be_visible()
        expect(page.locator(".issue-num")).to_contain_text("#456")


class TestTabSwitching:
    """Tests for tab switching functionality."""

    def test_work_tab_active_by_default(self, page: Page):
        """Work tab is active on page load."""
        work_tab = page.locator(".tab").first
        expect(work_tab).to_have_class("tab active")

    def test_switch_to_problems_tab(self, page: Page, web_server):
        """Click Problems tab switches view and updates URL."""
        # Add a failed item so problems tab has something
        web_server["orchestrator"].add_history_entry(100, "Failed Issue", "failed")
        page.reload()

        problems_tab = page.locator(".tab:has-text('Problems')")
        problems_tab.click()

        # URL should have tab=problems
        expect(page).to_have_url(re.compile(r"tab=problems"))


class TestSettingsMenu:
    """Tests for settings menu functionality."""

    def test_settings_menu_opens(self, page: Page):
        """Settings button opens dropdown menu."""
        settings_btn = page.locator(".settings-btn")
        settings_btn.click()

        menu = page.locator("#settingsMenu")
        expect(menu).to_have_class("settings-menu visible")

    def test_pause_resume_control(self, page: Page, web_server):
        """Pause menu item toggles orchestrator state."""
        # Open settings menu
        page.locator(".settings-btn").click()

        # Click pause
        page.locator("#pauseResumeItem").click()

        # Wait for API call
        page.wait_for_timeout(500)

        # Verify orchestrator was paused
        assert web_server["orchestrator"].state.paused is True

    def test_settings_menu_closes_on_click_outside(self, page: Page):
        """Settings menu closes when clicking elsewhere."""
        page.locator(".settings-btn").click()
        menu = page.locator("#settingsMenu")
        expect(menu).to_have_class(re.compile(r"visible"))

        # Click elsewhere
        page.locator("header h1").click()
        expect(menu).not_to_have_class(re.compile(r"visible"))


class TestContextMenus:
    """Tests for right-click context menus."""

    def test_right_click_shows_context_menu(self, page: Page, web_server):
        """Right-click on issue row shows context menu."""
        web_server["orchestrator"].add_queue_issue(123, "Test Issue")
        page.reload()

        row = page.locator(".issue-row").first
        row.click(button="right")

        menu = page.locator("#contextMenu")
        expect(menu).to_have_class(re.compile(r"visible"))

    def test_context_menu_closes_on_click_outside(self, page: Page, web_server):
        """Context menu closes when clicking elsewhere."""
        web_server["orchestrator"].add_queue_issue(123, "Test Issue")
        page.reload()

        row = page.locator(".issue-row").first
        row.click(button="right")

        menu = page.locator("#contextMenu")
        expect(menu).to_have_class(re.compile(r"visible"))

        # Click elsewhere
        page.locator("header").click()
        expect(menu).not_to_have_class(re.compile(r"visible"))

    def test_context_menu_has_github_links(self, page: Page, web_server):
        """Context menu shows Open Issue on GitHub option."""
        web_server["orchestrator"].add_queue_issue(123, "Test Issue")
        page.reload()

        row = page.locator(".issue-row").first
        row.click(button="right")

        issue_link = page.locator("#menuIssue")
        expect(issue_link).to_be_visible()
        expect(issue_link).to_contain_text("Open Issue on GitHub")

    def test_context_menu_retry_for_failed_items(self, page: Page, web_server):
        """Retry option visible for failed items in problems tab."""
        web_server["orchestrator"].add_history_entry(100, "Failed Issue", "failed")
        page.goto(f"{web_server['url']}?tab=problems")

        row = page.locator(".issue-row").first
        row.click(button="right")

        retry_item = page.locator("#menuRetry")
        expect(retry_item).to_be_visible()

    def test_context_menu_dismiss_for_history_items(self, page: Page, web_server):
        """Dismiss option visible for history items."""
        web_server["orchestrator"].add_history_entry(100, "Failed Issue", "failed")
        page.goto(f"{web_server['url']}?tab=problems")

        row = page.locator(".issue-row").first
        row.click(button="right")

        dismiss_item = page.locator("#menuDismiss")
        expect(dismiss_item).to_be_visible()


class TestModals:
    """Tests for modal dialogs."""

    def test_info_modal_opens(self, page: Page):
        """Settings > About opens info modal."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('About')").click()

        modal = page.locator("#modalOverlay")
        expect(modal).to_have_class(re.compile(r"visible"))
        expect(page.locator("#modalTitle")).to_contain_text("About")

    def test_info_modal_shows_version(self, page: Page):
        """Info modal displays version info."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('About')").click()

        modal_body = page.locator("#modalBody")
        expect(modal_body).to_contain_text("test/repo")

    def test_config_modal_opens(self, page: Page):
        """Settings > View Config opens config modal."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('View Config')").click()

        expect(page.locator("#modalTitle")).to_contain_text("Config")

    def test_debug_modal_opens(self, page: Page):
        """Settings > Debug opens debug modal."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('Debug')").click()

        expect(page.locator("#modalTitle")).to_contain_text("Debug")

    def test_modal_closes_on_x_button(self, page: Page):
        """Modal closes when X button clicked."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('About')").click()

        modal = page.locator("#modalOverlay")
        expect(modal).to_have_class(re.compile(r"visible"))

        page.locator(".modal-close").click()
        expect(modal).not_to_have_class(re.compile(r"visible"))

    def test_modal_closes_on_overlay_click(self, page: Page):
        """Modal closes when overlay background clicked."""
        page.locator(".settings-btn").click()
        page.locator(".settings-menu-item:has-text('About')").click()

        modal = page.locator("#modalOverlay")
        expect(modal).to_have_class(re.compile(r"visible"))

        # Click the overlay (not the modal content)
        modal.click(position={"x": 10, "y": 10})
        expect(modal).not_to_have_class(re.compile(r"visible"))


class TestPagination:
    """Tests for pagination functionality."""

    def test_pagination_hidden_with_single_page(self, page: Page, web_server):
        """Pagination not visible when queue fits on one page."""
        for i in range(5):
            web_server["orchestrator"].add_queue_issue(i + 1, f"Issue {i + 1}")
        page.reload()

        # With only 5 items (less than 20 per page), pagination is not rendered
        pagination = page.locator(".pagination")
        expect(pagination).to_have_count(0)

    def test_pagination_visible_with_many_items(self, page: Page, web_server):
        """Pagination visible with more than 20 queue items."""
        for i in range(25):
            web_server["orchestrator"].add_queue_issue(i + 1, f"Issue {i + 1}")
        page.reload()

        pagination = page.locator(".pagination")
        expect(pagination).to_contain_text("Page 1 of 2")

    def test_pagination_next_button(self, page: Page, web_server):
        """Next button navigates to page 2."""
        for i in range(25):
            web_server["orchestrator"].add_queue_issue(i + 1, f"Issue {i + 1}")
        page.reload()

        next_btn = page.locator(".pagination button:has-text('Next')")
        next_btn.click()

        expect(page).to_have_url(re.compile(r"page=2"))

    def test_pagination_prev_button(self, page: Page, web_server):
        """Previous button navigates back."""
        for i in range(25):
            web_server["orchestrator"].add_queue_issue(i + 1, f"Issue {i + 1}")
        page.goto(f"{web_server['url']}?page=2")

        prev_btn = page.locator(".pagination button:has-text('Prev')")
        prev_btn.click()

        expect(page).to_have_url(re.compile(r"page=1"))


class TestProblemsTab:
    """Tests for the Problems tab."""

    def test_problems_badge_shows_count(self, page: Page, web_server):
        """Problems tab badge shows correct count."""
        web_server["orchestrator"].add_history_entry(100, "Failed 1", "failed")
        web_server["orchestrator"].add_history_entry(101, "Blocked 1", "blocked")
        page.reload()

        badge = page.locator(".tab-badge")
        expect(badge).to_have_text("2")

    def test_problems_badge_empty_class(self, page: Page):
        """Problems badge has empty class when count is 0."""
        badge = page.locator(".tab-badge")
        expect(badge).to_have_class(re.compile(r"empty"))

    def test_failed_issue_in_problems_tab(self, page: Page, web_server):
        """Failed issues appear in problems tab."""
        web_server["orchestrator"].add_history_entry(100, "Failed Issue", "failed")
        page.goto(f"{web_server['url']}?tab=problems")

        expect(page.locator(".issue-row")).to_be_visible()
        expect(page.locator(".issue-num")).to_contain_text("#100")

    def test_blocked_issue_in_problems_tab(self, page: Page, web_server):
        """Blocked issues appear in problems tab."""
        web_server["orchestrator"].add_history_entry(101, "Blocked Issue", "blocked")
        page.goto(f"{web_server['url']}?tab=problems")

        expect(page.locator(".issue-row")).to_be_visible()
        expect(page.locator(".issue-num")).to_contain_text("#101")


class TestStatusIndicators:
    """Tests for status indicators and badges."""

    def test_starting_status_badge(self, page: Page, web_server):
        """Status badge shows Starting during startup."""
        web_server["orchestrator"].state.startup_status = "pending"
        page.reload()

        badge = page.locator(".status-badge.status-starting")
        expect(badge).to_be_visible()
        expect(badge).to_contain_text("Starting")

    def test_active_session_has_status_dot(self, page: Page, web_server):
        """Active session row has status dot."""
        web_server["orchestrator"].add_active_session(123, "Active Issue")
        page.reload()

        status_dot = page.locator(".issue-row .status-dot")
        expect(status_dot).to_be_visible()


class TestSSEConnection:
    """Tests for Server-Sent Events connection."""

    def test_sse_endpoint_responds(self, page: Page, web_server):
        """SSE endpoint is accessible and returns event stream."""
        # SSE endpoints stream forever, so we use HEAD to check availability
        # or fetch with a short timeout and check headers
        import httpx

        # Use httpx with timeout to verify SSE endpoint exists and has correct content-type
        with httpx.Client() as client:
            # Make request with stream=True so we can check headers without waiting for body
            with client.stream(
                "GET", f"{web_server['url']}/api/events", timeout=5.0
            ) as response:
                assert response.status_code == 200
                content_type = response.headers.get("content-type", "")
                assert "text/event-stream" in content_type


class TestHistoryDisplay:
    """Tests for session history display."""

    def test_completed_session_shows_in_work_tab(self, page: Page, web_server):
        """Completed sessions appear in work tab history."""
        web_server["orchestrator"].add_history_entry(200, "Completed Issue", "completed")
        page.reload()

        expect(page.locator(".issue-row")).to_be_visible()
        expect(page.locator(".issue-num")).to_contain_text("#200")

    def test_history_shows_runtime(self, page: Page, web_server):
        """History entries show runtime."""
        web_server["orchestrator"].add_history_entry(200, "Completed Issue", "completed")
        page.reload()

        time_cell = page.locator(".issue-time")
        expect(time_cell).to_contain_text("15")  # 15 minutes from mock
