"""Flow-first browser smoke tests for dashboard runtime behavior."""

from __future__ import annotations

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


def test_issue_drawer_validation_renders_structured_view_for_junit_cases(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """When the issue-detail run_diagnostic carries parsed JUnit cases, the
    drawer renders the test-centric layout (headline + filter chips +
    rows) and hides the legacy failed-test-name <ul>. Per-row click
    expands inline and the rendered error text matches the longrepr.

    Drives ``renderIssueDetailValidation`` directly via page.evaluate to
    decouple the assertion from the orchestrator's payload shape: the
    only contract this test cares about is "given a diagnostic with
    junit_cases, the drawer renders structured content with the right
    counts/names/errors."
    """
    _goto_dashboard(page, str(web_server["url"]))

    expected_failed_label = "test_circuit_open"
    expected_passed_label = "test_circuit_closed"
    expected_skipped_label = "test_only_on_linux"
    expected_longrepr = (
        "AssertionError: expected 1 open circuit but got 0\n"
        "  in tests/unit/test_circuits.py:42"
    )

    synthetic = {
        "issue_number": 408,
        "title": "Validation diagnostic test",
        "actions": [
            {
                "id": "open_validation_failure",
                "label": "Validation Details",
                "run_dir": "/tmp/synthetic-run-dir",
            }
        ],
        "summary": {
            "run_diagnostic": {
                "state": "validation_failed",
                "run_dir": "/tmp/synthetic-run-dir",
                "session_name": "coding-1",
                "reason": "tests failed",
                "suite": "publish_gate",
                "command": "make test",
                "exit_code": 1,
                "failed_tests": [f"tests/unit/test_circuits.py::{expected_failed_label}"],
                "failed_tests_preview": [f"tests/unit/test_circuits.py::{expected_failed_label}"],
                "validation_record_path": None,
                "validation_stderr": None,
                "validation_stdout": None,
                "junit_cases": [
                    {
                        "nodeid": f"tests/unit/test_circuits.py::{expected_failed_label}",
                        "case_id": f"tests/unit/test_circuits.py::{expected_failed_label}",
                        "label": expected_failed_label,
                        "display_name": expected_failed_label,
                        "suite_name": "tests/unit/test_circuits.py",
                        "outcome": "failed",
                        "retry_outcome": None,
                        "duration_seconds": 0.42,
                        "longrepr": expected_longrepr,
                        "failure_summary": "AssertionError: expected 1 open circuit but got 0",
                        "history": [],
                        "existing_issue": None,
                        "category": "failed",
                        "flip_rate": 0.0,
                        "flip_rate_percent": 0.0,
                        "is_likely_flaky": False,
                        "is_quarantined": False,
                        "result_source": "junit",
                        "updated_at": "",
                    },
                    {
                        "nodeid": f"tests/unit/test_circuits.py::{expected_passed_label}",
                        "case_id": f"tests/unit/test_circuits.py::{expected_passed_label}",
                        "label": expected_passed_label,
                        "display_name": expected_passed_label,
                        "suite_name": "tests/unit/test_circuits.py",
                        "outcome": "passed",
                        "retry_outcome": None,
                        "duration_seconds": 0.18,
                        "longrepr": None,
                        "failure_summary": None,
                        "history": [],
                        "existing_issue": None,
                        "category": "passed",
                        "flip_rate": 0.0,
                        "flip_rate_percent": 0.0,
                        "is_likely_flaky": False,
                        "is_quarantined": False,
                        "result_source": "junit",
                        "updated_at": "",
                    },
                    {
                        "nodeid": f"tests/unit/test_circuits.py::{expected_skipped_label}",
                        "case_id": f"tests/unit/test_circuits.py::{expected_skipped_label}",
                        "label": expected_skipped_label,
                        "display_name": expected_skipped_label,
                        "suite_name": "tests/unit/test_circuits.py",
                        "outcome": "skipped",
                        "retry_outcome": None,
                        "duration_seconds": 0.0,
                        "longrepr": "platform-only test",
                        "failure_summary": None,
                        "history": [],
                        "existing_issue": None,
                        "category": "skipped",
                        "flip_rate": 0.0,
                        "flip_rate_percent": 0.0,
                        "is_likely_flaky": False,
                        "is_quarantined": False,
                        "result_source": "junit",
                        "updated_at": "",
                    },
                ],
            }
        },
    }

    # Show the drawer container so child elements are visible during render.
    page.evaluate(
        """payload => {
            const drawer = document.getElementById('issueDetailDrawer');
            drawer.classList.add('visible');
            drawer.setAttribute('aria-hidden', 'false');
            window.renderIssueDetailValidation(payload);
        }""",
        synthetic,
    )

    drawer = page.locator("#issueDetailDrawer.visible")
    expect(drawer).to_be_visible(timeout=5000)
    validation = page.locator("#issueDetailValidation")
    expect(validation).to_be_visible(timeout=5000)
    expect(page.locator("#issueDetailValidationReason")).to_contain_text(
        "tests failed • Command: make test"
    )

    # ── Legacy failed-test-name <ul> must be hidden when junit_cases drives the view ──
    tests_ul = page.locator("#issueDetailValidationTests")
    expect(tests_ul).to_have_css("display", "none")

    # ── Structured panel must render with correct counts ──
    structured = page.locator("#issueDetailValidationStructured")
    expect(structured).to_be_visible()
    headline = structured.locator(".test-results-headline")
    expect(headline).to_be_visible()
    expect(headline).to_contain_text("3 total")
    expect(headline).to_contain_text("1 passed")
    expect(headline).to_contain_text("1 failing")
    expect(headline).to_contain_text("1 skipped")

    # ── One row per junit_case, with right names ──
    rows = structured.locator(".trr-row")
    expect(rows).to_have_count(3)
    rows_text = " ".join(rows.all_text_contents())
    assert expected_failed_label in rows_text
    assert expected_passed_label in rows_text
    assert expected_skipped_label in rows_text

    # ── Filter chip toggles ──
    failing_chip = structured.locator(".trf-chip[data-filter='failing']")
    expect(failing_chip).to_be_visible()
    failing_chip.click()
    visible_after_failing = [
        r for r in rows.all() if r.evaluate("el => el.style.display !== 'none'")
    ]
    assert len(visible_after_failing) == 1
    assert expected_failed_label in (visible_after_failing[0].text_content() or "")

    # Switch back to All to set up expand assertion
    structured.locator(".trf-chip[data-filter='all']").click()

    # ── Click failing row → expand reveals the *specific* longrepr text ──
    failing_row = structured.locator(
        f".trr-row[data-nodeid='tests/unit/test_circuits.py::{expected_failed_label}']"
    )
    expect(failing_row).to_have_attribute("data-expandable", "1")
    failing_row.locator(".trr-row-main").click()
    error_pre = failing_row.locator(".trr-error-text")
    expect(error_pre).to_be_visible()
    expect(error_pre).to_contain_text("AssertionError: expected 1 open circuit but got 0")
    expect(error_pre).to_contain_text("test_circuits.py:42")

    # ── Issue-session validation must NOT render an inline lifecycle block:
    # validation cases have no `existing_issue` (E2E flake-tracking is the
    # only producer of those links). This keeps the drawer's row UI free
    # of E2E-specific affordances that would not have working endpoints.
    expect(failing_row.locator(".trr-lifecycle")).to_have_count(0)

    # ── Passed row is not expandable (no error, no lifecycle) ──
    passed_row = structured.locator(
        f".trr-row[data-nodeid='tests/unit/test_circuits.py::{expected_passed_label}']"
    )
    expect(passed_row).to_have_attribute("data-expandable", "0")


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
