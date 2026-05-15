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

    run_row = journey.locator("details.journey-run").first
    cycle_row = journey.locator("details.journey-cycle").first
    expect(run_row).to_be_visible()
    expect(run_row).to_have_attribute("open", "")
    expect(run_row).not_to_have_attribute("data-lifecycle-command", re.compile(".+"))
    expect(run_row.locator(":scope > summary .hierarchical-timeline-caret")).to_have_count(1)
    expect(cycle_row).to_be_visible()
    expect(cycle_row).to_have_attribute("open", "")
    expect(cycle_row).not_to_have_attribute("data-lifecycle-command", re.compile(".+"))
    expect(cycle_row.locator(":scope > summary .hierarchical-timeline-caret")).to_have_count(1)

    cycle_body = cycle_row.locator(":scope > .journey-cycle-body")
    expect(cycle_body).to_be_visible()
    cycle_row.locator(":scope > summary").click()
    expect(cycle_row).not_to_have_attribute("open", "")
    expect(cycle_body).to_be_hidden()
    cycle_row.locator(":scope > summary").click()
    expect(cycle_row).to_have_attribute("open", "")
    expect(cycle_body).to_be_visible()


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
    on every row, ``role="group"`` on every children container — and on
    every triage card so failed-run children share the same tree/group
    ownership model as the browse rows.
    ``enhanceCanonicalValidationViewerAccessibility`` (called after
    mount by the drawer) fills in aria-level / aria-setsize /
    aria-posinset and wires the WAI-ARIA keyboard contract.

    The matrix of keyboard commands is covered by JS-vm tests via the
    pure ``_treeCommandForKey`` translator and the dependency-injected
    ``_executeTreeCommand`` executor (with a fake tree fixture).  This
    test is the thin browser smoke that proves the wire-up works under
    a real keypress pipeline: it focuses a treeitem with
    ``element.focus()`` (real focus, not synthetic dispatch) and uses
    ``page.keyboard.press(...)`` for real keypresses.

    Uses a *failed* payload (reviewer Blocker 1 on PR #6316) so the
    triage-card treeitems are exercised and we can assert their
    position metadata is complete.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Failed payload: two failing tests + one passing test so we cover
    # both the triage hierarchy (under .cvv-triage-card[role=group]) and
    # the browse-by-file hierarchy (under .cvv-row-browse[role=treeitem]).
    page.route(
        "**/api/dialog/validation-failure/410**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body="""
            {
              "title": "Validation Results #410",
              "reason": "2 tests failed",
              "suite": "publish_gate",
              "command": "make validate-pr",
              "exit_code": 2,
              "started_at": "2026-01-01T13:00:00Z",
              "ended_at": "2026-01-01T13:06:00Z",
              "status": "failed",
              "failed_tests": ["tests/test_a.py::test_alpha", "tests/test_a.py::test_gamma"],
              "junit_cases": [
                {"case_id": "a", "display_name": "test_alpha", "suite_name": "tests/test_a.py",
                 "outcome": "failed", "duration_seconds": 0.003,
                 "failure_details": "AssertionError: bad\\n  at frame 1",
                 "system_out": "before", "system_err": "after", "extras": []},
                {"case_id": "c", "display_name": "test_gamma", "suite_name": "tests/test_a.py",
                 "outcome": "failed", "duration_seconds": 0.004,
                 "failure_details": "AssertionError: also bad\\n  at frame 2",
                 "extras": []},
                {"case_id": "b", "display_name": "test_beta", "suite_name": "tests/test_b.py",
                 "outcome": "passed", "duration_seconds": 0.004, "extras": []}
              ],
              "stdout_excerpt": [],
              "stderr_excerpt": [],
              "summary_rows": [{"label": "Outcome", "value": "Failed"}],
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
    # Phase D redesign (issue #6322): the triage card is now itself a
    # treeitem (the outer <details>), and its body (.cvv-triage-body)
    # carries role="group" so child leaf rows have a proper parent
    # group for aria-setsize/posinset enumeration.  Asserts on both.
    triage_cards = cvv.locator(".cvv-triage-card")
    assert triage_cards.count() >= 1, "failed payload should render at least one triage card"
    expect(triage_cards.first).to_have_attribute("role", "treeitem")
    triage_body = triage_cards.first.locator(".cvv-triage-body")
    expect(triage_body).to_have_attribute("role", "group")

    treeitems = cvv.locator('[role="treeitem"]')
    assert treeitems.count() >= 1
    # Every treeitem has complete position metadata.  This is the
    # specific regression the reviewer caught: failed-triage treeitems
    # previously had aria-setsize but no aria-posinset.
    levels = treeitems.evaluate_all(
        "elements => elements.map(el => [el.getAttribute('aria-level'),"
        " el.getAttribute('aria-setsize'), el.getAttribute('aria-posinset')])"
    )
    for level, setsize, posinset in levels:
        assert level is not None and int(level) >= 1, f"aria-level missing: {level}"
        assert setsize is not None and int(setsize) >= 1, f"aria-setsize missing: {setsize}"
        assert posinset is not None and int(posinset) >= 1, f"aria-posinset missing: {posinset}"

    # Within each triage card's BODY (Phase D: cvv-triage-body wraps
    # the child treeitems), the children's posinset values form a
    # contiguous 1..N sequence — that's what a screen reader uses to
    # announce position-within-set.
    triage_child_meta = cvv.evaluate(
        "root => Array.from(root.querySelectorAll('.cvv-triage-card')).map(card =>"
        "  Array.from(card.querySelectorAll(':scope > .cvv-triage-body > [role=\"treeitem\"]')).map(el => ({"
        "    setsize: el.getAttribute('aria-setsize'),"
        "    posinset: el.getAttribute('aria-posinset'),"
        "  }))"
        ")"
    )
    for card_children in triage_child_meta:
        assert len(card_children) > 0, "triage card should contain at least one treeitem"
        for idx, meta in enumerate(card_children, start=1):
            assert meta["setsize"] == str(len(card_children))
            assert meta["posinset"] == str(idx)

    # Exactly one treeitem in the tab order (roving tabindex).
    tab_stops = treeitems.evaluate_all(
        "elements => elements.filter(el => el.tabIndex === 0).length"
    )
    assert tab_stops == 1, f"expected exactly 1 tab-stop in tree, got {tab_stops}"

    # ── Real keyboard input on the mounted viewer ───────────────────
    # The matrix is JS-vm-tested via _treeCommandForKey and
    # _executeTreeCommand.  Here we prove that real keypresses flow
    # through the wire-up: focus the tab-stop, press ArrowDown, and
    # verify focus moves to a different treeitem.
    page.evaluate(
        "() => {"
        "  const el = document.querySelector('.journey-step-validation-body .cvv-root [role=\"treeitem\"][tabindex=\"0\"]');"
        "  if (!el) throw new Error('no tab-stop treeitem found');"
        "  el.focus();"
        "}"
    )
    # Identify the focused treeitem by its index in the tree's
    # treeitem list rather than outerHTML — the canonical viewer's
    # rows share enough markup that an 80-char prefix slice can
    # collide between two siblings.  Index-in-tree is unique.
    active_index_script = (
        "() => {"
        "  const all = Array.from(document.querySelectorAll('.journey-step-validation-body .cvv-root [role=\"treeitem\"]'));"
        "  return all.indexOf(document.activeElement);"
        "}"
    )
    first_index = page.evaluate(active_index_script)
    assert first_index >= 0, f"first treeitem should be focused, got index {first_index}"
    page.keyboard.press("ArrowDown")
    after_index = page.evaluate(active_index_script)
    assert after_index != first_index and after_index >= 0, (
        f"ArrowDown should move focus to a different treeitem; before index={first_index}, after index={after_index}"
    )
    after_down_role = page.evaluate(
        "() => document.activeElement && document.activeElement.getAttribute('role')"
    )
    assert after_down_role == "treeitem", f"ArrowDown should land on a treeitem, got role={after_down_role}"

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
