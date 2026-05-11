"""Flow-first browser smoke tests for dashboard runtime behavior."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect


def _goto_dashboard(page: Page, base_url: str) -> None:
    """Open the dashboard with a navigation budget that survives full-suite load."""
    page.goto(base_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def _wait_for_issue_detail_hydration(page: Page) -> None:
    drawer = page.locator("#issueDetailDrawer.visible")
    expect(drawer).to_be_visible()
    expect(drawer).to_have_attribute("data-lifecycle-kind", "dashboard")
    journey = page.locator("#issueDetailJourney")
    expect(journey.locator(".journey-run").first).to_be_visible(timeout=5000)
    expect(journey.locator(".journey-cycle").first).to_be_visible(timeout=5000)


def test_dashboard_loads_without_page_errors(page: Page, web_server: dict[str, object]) -> None:
    """Dashboard JS should execute without runtime exceptions on initial load."""
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    _goto_dashboard(page, str(web_server["url"]))

    expect(page.locator("#tab-dashboard")).to_be_visible()
    assert errors == []


def test_flow_card_opens_issue_detail_drawer(page: Page, web_server: dict[str, object]) -> None:
    """Clicking a flow card focus button opens the issue detail drawer."""
    _goto_dashboard(page, str(web_server["url"]))

    card_focus = page.locator(".dashboard-columns .issue-card[data-issue='408'] .card-focus").first
    expect(card_focus).to_be_visible()
    card_focus.click()

    _wait_for_issue_detail_hydration(page)
    drawer = page.locator("#issueDetailDrawer.visible")
    expect(page.locator("#issueDetailTitle")).to_contain_text("Flow smoke item")
    expect(drawer).to_have_attribute("data-lifecycle-iterations", "1")


def test_running_card_surfaces_timeline_snapshot(page: Page, web_server: dict[str, object]) -> None:
    """Running cards should show the latest timeline narrative as a snapshot."""
    _goto_dashboard(page, str(web_server["url"]))

    running_card = page.locator(".dashboard-columns .issue-card[data-issue='409']").first
    expect(running_card).to_be_visible()
    expect(running_card).to_contain_text("Working on running timeline snapshot")


def test_issue_card_timeline_button_opens_cycle_timeline(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """The visible card Timeline affordance opens the cycle-aware drawer."""
    _goto_dashboard(page, str(web_server["url"]))

    timeline_btn = page.locator(
        ".dashboard-columns .issue-card[data-issue='408'] .card-timeline-btn"
    ).first
    expect(timeline_btn).to_be_visible(timeout=5000)
    timeline_btn.click()

    _wait_for_issue_detail_hydration(page)
    drawer = page.locator("#issueDetailDrawer.visible")
    expect(page.locator("#issueDetailTitle")).to_contain_text("Flow smoke item")

    journey = page.locator("#issueDetailJourney")
    expect(page.locator("#issueDetailTimelineHeading")).to_be_focused()
    expect(journey).to_contain_text("Cycle 1")
    expect(journey).to_contain_text("Coding")
    expect(journey).to_contain_text("Review")
    expect(journey).to_contain_text("Agent finished coding")
    expect(journey).to_contain_text("Review approved")
    expect(journey.locator(".timeline-empty")).to_have_count(0)


def test_e2e_tab_navigation_works(page: Page, web_server: dict[str, object]) -> None:
    """Dashboard tabs should navigate to E2E view."""
    _goto_dashboard(page, str(web_server["url"]))

    page.locator("#tab-e2e").click(no_wait_after=True)
    page.wait_for_url("**?tab=e2e**", timeout=90_000)
    expect(page.locator("#panel-e2e")).to_be_visible()


def test_validation_badge_click_dispatches_through_command_pipeline(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """End-to-end proof of the typed ``CycleValidationBadge`` chain (issue
    #6310 AC-2).

    Exercises the full path the previous coverage only hit in pieces:

    1. Backend builds ``IssueCycle.validation = CycleValidationBadge(
       state='passed', command=OpenValidationDetailsCommand(...))`` from
       seeded events (``session.completed`` + ``validation.passed``).
    2. ``.model_dump`` serializes the typed badge onto the wire.
    3. The drawer reads the typed payload and renders a green
       ``✓ Validated`` button carrying ``data-lifecycle-command`` JSON.
    4. Clicking the button routes through the shared
       ``runE2ELifecycleCommand`` dispatcher in ``lifecycle_commands.js``.
    5. ``runE2ELifecycleCommand`` dispatches to ``openValidationFailure``
       which fetches the dialog endpoint.
    6. The validation dialog renders with the expected body.

    The JS-vm tests stub step 5; this test does it in a real browser,
    closing the seam the earlier JS-vm + Python tests left implicit.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the dialog endpoint just like the existing dialog test —
    # ``openValidationFailure`` will fetch it after the badge click.
    page.route(
        "**/api/dialog/validation-failure/410**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body="""
            {
              "title": "Validation Results #410",
              "reason": "Validation passed for run-410",
              "suite": "publish_gate",
              "command": "make validate-pr",
              "exit_code": 0,
              "started_at": "2026-01-01T13:00:00Z",
              "ended_at": "2026-01-01T13:06:00Z",
              "failed_tests": [],
              "stdout_excerpt": ["all checks green"],
              "stderr_excerpt": [],
              "summary_rows": [
                {"label": "Reason", "value": "Validation passed"},
                {"label": "Exit Code", "value": "0"}
              ],
              "action_sections": []
            }
            """,
        ),
    )

    _goto_dashboard(page, str(web_server["url"]))

    # Open the drawer for issue 410 (the validated-cycle fixture).
    card_focus = page.locator(
        ".dashboard-columns .issue-card[data-issue='410'] .card-focus"
    ).first
    expect(card_focus).to_be_visible()
    card_focus.click()

    _wait_for_issue_detail_hydration(page)

    # Step 3 + 4: the drawer rendered the typed-Command badge.  The
    # passed-state class proves the backend derived ``state='passed'``
    # from the seeded ``validation.passed`` event, and the
    # ``data-lifecycle-command`` attribute carries the typed
    # ``OpenValidationDetailsCommand`` payload — without it, the
    # ``runE2ELifecycleCommand`` dispatcher would have nothing to route.
    badge = page.locator(".journey-cycle-validation-badge.is-passed").first
    expect(badge).to_be_visible(timeout=5000)
    expect(badge).to_contain_text("✓ Validated")
    expect(badge).to_have_attribute(
        "data-lifecycle-command",
        re.compile(r'"kind":\s*"open_validation_details"'),
    )

    # Step 5 + 6: clicking the typed-Command button goes through the
    # shared dispatcher and opens the dialog.
    badge.click()
    modal = page.locator("#modalOverlay.visible")
    expect(modal).to_be_visible(timeout=5000)
    expect(page.locator("#modalTitle")).to_contain_text("Validation Results #410")
    expect(page.locator("#modalBody")).to_contain_text("Validation passed")

    assert errors == []


def test_validation_failure_dialog_renders_results_and_artifacts(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Validation dialog should present results first and grouped artifacts second."""
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.route(
        "**/api/dialog/validation-failure/408**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body="""
            {
              "title": "Validation Failure #408",
              "reason": "Validation failed for abc123 (exit_code=2)",
              "suite": "publish_gate",
              "command": "make validate-pr",
              "exit_code": 2,
              "started_at": "2026-04-24T00:00:00Z",
              "ended_at": "2026-04-24T00:01:00Z",
              "failed_tests": [
                "tests/unit/test_example.py::test_breaks",
                "tests/unit/test_example.py::test_still_breaks"
              ],
              "stdout_excerpt": [
                "FAILED tests/unit/test_example.py::test_breaks"
              ],
              "stderr_excerpt": [
                "make: *** [validate-pr] Error 2"
              ],
              "summary_rows": [
                {"label": "Reason", "value": "Validation failed for abc123 (exit_code=2)"},
                {"label": "Suite", "value": "publish_gate"},
                {"label": "Command", "value": "make validate-pr"},
                {"label": "Exit Code", "value": "2"},
                {"label": "Failing Tests", "value": "2"}
              ],
              "action_sections": [
                {
                  "title": "Validation Artifacts",
                  "actions": [
                    {"type": "open_path", "label": "Open Validation Record", "path": "/tmp/validation-record.json"},
                    {"type": "open_path", "label": "Open Validation Output", "path": "/tmp/validation-output.log"}
                  ]
                },
                {
                  "title": "Session Evidence",
                  "actions": [
                    {"type": "open_agent_log", "label": "View Session Recording", "issue_number": 408, "run_dir": "/tmp/run-408"}
                  ]
                },
                {
                  "title": "Diagnostics",
                  "actions": [
                    {"type": "open_session_diagnostics", "label": "Full Diagnostics", "issue_number": 408, "run_dir": "/tmp/run-408"}
                  ]
                }
              ]
            }
            """,
        ),
    )

    page.goto(str(web_server["url"]), wait_until="domcontentloaded")
    page.evaluate("openValidationFailure(408, '/tmp/run-408', 'modal')")

    modal = page.locator("#modalOverlay.visible")
    expect(modal).to_be_visible(timeout=5000)
    expect(page.locator("#modalTitle")).to_contain_text("Validation Failure #408")
    expect(page.locator("#modalBody")).to_contain_text("Validation Results")
    expect(page.locator("#modalBody")).to_contain_text("Results")
    expect(page.locator("#modalBody")).to_contain_text("Artifacts")
    expect(page.locator("#modalBody")).to_contain_text("Validation Artifacts")
    expect(page.locator("#modalBody")).to_contain_text("Session Evidence")
    expect(page.locator("#modalBody")).to_contain_text("Full Diagnostics")
    expect(page.locator("#modalBody")).to_contain_text("tests/unit/test_example.py::test_breaks")
    expect(page.locator("#modalBody .diag-validation-action-group")).to_have_count(3)

    assert errors == []
