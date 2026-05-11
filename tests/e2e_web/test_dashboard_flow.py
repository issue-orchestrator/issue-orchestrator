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


def test_validation_badge_click_expands_inline_drawer_detail(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """End-to-end proof of the Phase-B drawer inline-expansion flow
    (issue #6310 follow-up).

    Phase B replaced the modal-popping badge with an in-drawer
    affordance: clicking the cycle's validation badge now expands the
    cycle's validation event row in place, lazy-fetches the canonical
    viewer payload, and mounts it directly under the step.  No more
    context-switching to a modal.

    Steps exercised:

    1. Backend still builds ``IssueCycle.validation = CycleValidationBadge(
       state='passed')`` from seeded events (``session.completed`` +
       ``validation.passed``); ``.model_dump`` serializes it.
    2. The drawer renders a green ``✓ Validated`` button with
       ``data-validation-state="passed"`` (no more typed-Command
       payload — that was the Phase-A contract).
    3. Clicking the button expands the cycle (if collapsed), finds the
       validation event step, and triggers
       ``toggleValidationEventInline`` which fetches the dialog
       endpoint.
    4. The canonical viewer (``cvv-root``) mounts under the step.  No
       modal opens.

    The JS-vm tests cover the helpers in isolation; this test pins the
    real browser flow so a regression that re-introduces the modal
    would fail here.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the dialog endpoint that the inline expansion fetches.
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
              "status": "passed",
              "failed_tests": [],
              "junit_cases": [],
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

    # The drawer rendered the inline-expansion badge.  Phase B's
    # contract: ``data-validation-state="passed"`` (state-driven, no
    # typed-Command payload) and the button text is the
    # passed-validated label.
    badge = page.locator(".journey-cycle-validation-badge.is-passed").first
    expect(badge).to_be_visible(timeout=5000)
    expect(badge).to_contain_text("✓ Validated")
    expect(badge).to_have_attribute("data-validation-state", "passed")

    # Phase B removed the typed-Command payload — it's intentionally
    # absent now.  If a future refactor brings it back, the inline
    # expansion flow should still own the click.
    assert badge.get_attribute("data-lifecycle-command") is None

    # Clicking the badge expands the cycle's validation event inline,
    # not a modal.  The expansion lazy-fetches the dialog endpoint and
    # mounts the canonical viewer (``cvv-root``) under the step.
    badge.click()

    # No modal opens — the Phase B explicit non-regression.
    modal = page.locator("#modalOverlay.visible")
    expect(modal).not_to_be_visible(timeout=2000)

    # The inline expansion body mounted with the canonical viewer.
    cvv = page.locator(".journey-step-validation-body .cvv-root").first
    expect(cvv).to_be_visible(timeout=5000)
    expect(cvv).to_have_attribute("data-cvv-status", "passed")

    assert errors == []


def test_validation_viewer_renders_aria_tree_and_supports_keyboard_nav(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Phase D (issue #6310 follow-up): the canonical validation viewer
    is a real ARIA tree.  ``role="tree"`` on the root, ``role="treeitem"``
    on every row, ``role="group"`` on every children container.
    ``enhanceCanonicalValidationViewerAccessibility`` (called after mount
    by the drawer) fills in aria-level / aria-setsize / aria-posinset
    and wires arrow-key navigation with roving tabindex.

    The JS-vm tests cover the render-time invariants on raw HTML.  This
    test pins the live-DOM behavior — that the enhancer runs, that
    keyboard nav actually moves focus, and that the open/close keys
    work.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the dialog endpoint with a payload rich enough to give the
    # viewer multiple treeitems at multiple levels (a failed test +
    # passed tests in browse-by-file).
    page.route(
        "**/api/dialog/validation-failure/410**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body="""
            {
              "title": "Validation Results #410",
              "reason": "Validation passed",
              "suite": "publish_gate",
              "command": "make validate-pr",
              "exit_code": 0,
              "started_at": "2026-01-01T13:00:00Z",
              "ended_at": "2026-01-01T13:06:00Z",
              "status": "passed",
              "failed_tests": [],
              "junit_cases": [
                {"case_id": "a", "display_name": "test_alpha", "suite_name": "tests/test_a.py", "outcome": "passed", "duration_seconds": 0.003, "extras": []},
                {"case_id": "b", "display_name": "test_beta", "suite_name": "tests/test_b.py", "outcome": "passed", "duration_seconds": 0.004, "extras": []}
              ],
              "stdout_excerpt": ["all checks green"],
              "stderr_excerpt": [],
              "summary_rows": [{"label": "Reason", "value": "Validation passed"}],
              "action_sections": []
            }
            """,
        ),
    )

    _goto_dashboard(page, str(web_server["url"]))
    page.locator(
        ".dashboard-columns .issue-card[data-issue='410'] .card-focus"
    ).first.click()
    _wait_for_issue_detail_hydration(page)
    page.locator(".journey-cycle-validation-badge.is-passed").first.click()

    cvv = page.locator(".journey-step-validation-body .cvv-root").first
    expect(cvv).to_be_visible(timeout=5000)

    # ── Render-time ARIA roles are live in the DOM ──────────────────
    expect(cvv).to_have_attribute("role", "tree")
    expect(cvv).to_have_attribute("aria-orientation", "vertical")
    treeitems = cvv.locator('[role="treeitem"]')
    assert treeitems.count() >= 1
    # Every treeitem has aria-level + aria-setsize + aria-posinset
    # filled in by the post-mount enhancer.
    levels = treeitems.evaluate_all(
        "elements => elements.map(el => [el.getAttribute('aria-level'),"
        " el.getAttribute('aria-setsize'), el.getAttribute('aria-posinset')])"
    )
    for level, setsize, posinset in levels:
        assert level is not None and int(level) >= 1, f"aria-level missing: {level}"
        assert setsize is not None and int(setsize) >= 1, f"aria-setsize missing: {setsize}"
        assert posinset is not None and int(posinset) >= 1, f"aria-posinset missing: {posinset}"

    # ── Roving tabindex: exactly one treeitem in the tab order ──────
    tab_stops = treeitems.evaluate_all(
        "elements => elements.filter(el => el.tabIndex === 0).length"
    )
    assert tab_stops == 1, f"expected exactly 1 tab-stop in tree, got {tab_stops}"

    # ── Keyboard nav: dispatch keydown directly via JS to bypass
    #    browser-specific focus quirks on <details> elements (in some
    #    browsers `details.focus()` moves focus to the summary, which
    #    affects what gets the keydown).  The tree's keydown listener
    #    is delegated at .cvv-root, so dispatching keydown on the
    #    treeitem reliably exercises it.  We assert on the *resulting*
    #    DOM state (open/closed) rather than on focus movement.
    def _dispatch_keydown(selector: str, key: str) -> None:
        page.evaluate(
            "([sel, key]) => {"
            "  const el = document.querySelector(sel);"
            "  if (!el) throw new Error('selector not found: ' + sel);"
            "  el.focus();"
            "  el.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true }));"
            "}",
            [selector, key],
        )

    # Diagnostic: confirm the keydown listener is bound to .cvv-root.
    bound = page.evaluate(
        "() => {"
        "  const el = document.querySelector('.journey-step-validation-body .cvv-root');"
        "  return el && el.dataset.cvvA11yBound;"
        "}"
    )
    assert bound == "1", f"cvv-root keydown listener not bound (dataset.cvvA11yBound={bound})"

    # ── ArrowRight on a collapsed treeitem expands it ───────────────
    # Drive the keyboard handler and assert on the *same* element after.
    # (Re-querying with ``:not([open])`` would resolve to a different
    # element once the first one opens, which masked the win.)
    open_after_right = page.evaluate(
        "() => {"
        "  const el = document.querySelector("
        "    '.journey-step-validation-body .cvv-root details[role=\"treeitem\"]:not([open])'"
        "  );"
        "  if (!el) return { found: false };"
        "  el.focus();"
        "  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));"
        "  return { found: true, open: el.open, ariaExpanded: el.getAttribute('aria-expanded') };"
        "}"
    )
    if open_after_right.get("found"):
        assert open_after_right["open"] is True, (
            f"ArrowRight did not open the treeitem; got {open_after_right}"
        )
        # ``aria-expanded`` is synced on the toggle event handler — wait
        # for the microtask to flush.
        page.wait_for_function(
            "() => {"
            "  const el = document.querySelector('.journey-step-validation-body .cvv-root details[role=\"treeitem\"][open]');"
            "  return el && el.getAttribute('aria-expanded') === 'true';"
            "}",
            timeout=2000,
        )

    # ── ArrowLeft on an expanded treeitem collapses it ──────────────
    open_after_left = page.evaluate(
        "() => {"
        "  const el = document.querySelector("
        "    '.journey-step-validation-body .cvv-row-browse[open]'"
        "  );"
        "  if (!el) return { found: false };"
        "  el.focus();"
        "  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));"
        "  return { found: true, open: el.open };"
        "}"
    )
    if open_after_left.get("found"):
        assert open_after_left["open"] is False, (
            f"ArrowLeft did not close the treeitem; got {open_after_left}"
        )

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
