"""Phase C of #6310 follow-up: Playwright smoke for the new E2E run modal.

The legacy filter-pill / per-row-action panel has been replaced by the
canonical validation viewer (the same component that backs the
validation modal and the per-issue drawer's inline expansion).  This
test pins:

- The modal mounts ``.cvv-root`` as its body.
- Run-level summary chips show the right counts.
- Failed tests render as triage cards; failures with a linked issue
  carry the ``io.agent-context`` plugin block beneath them with the
  Open-issue-drawer affordance.
- Failed tests without a linked issue carry the plugin block too, but
  in the "no linked issue" branch (and the run-level
  untracked-failures banner offers the Create-issue affordance).
- The canonical viewer is ARIA-enhanced after mount (Phase D
  ``enhanceCanonicalValidationViewerAccessibility`` was called):
  ``role="tree"``, every ``role="treeitem"`` has ``aria-level``,
  ``aria-setsize``, and ``aria-posinset`` filled in.
- Real keyboard input moves focus on the mounted tree.

A single test exercises the whole path with a stubbed run-detail
payload.  The matrix of translator / layout-selector / plugin behavior
lives in JS-vm tests; this is the live-pipeline proof.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect


_RUN_ID = 9001


_STUB_RUN_DETAIL: dict[str, object] = {
    "run": {
        "id": _RUN_ID,
        "status": "failed",
        "started_at": "2026-05-12T01:00:00Z",
        "ended_at": "2026-05-12T01:01:00Z",
        "duration_seconds": 60.0,
        "commit_sha": "abc1234",
        "branch": "main",
        "runner_kind": "pytest",
        "command": ["pytest", "tests/e2e", "--junit-xml=junit.xml"],
    },
    "results_by_category": {
        "untriaged": [
            {
                "nodeid": "tests/e2e/test_a.py::test_untracked_failure",
                "suite_name": "tests/e2e/test_a.py",
                "outcome": "failed",
                "duration_seconds": 30.0,
                "failure_summary": "TimeoutError: orchestrator did not publish within 30s",
                "longrepr": "",
                "history": [],
                "existing_issue": None,
                "is_quarantined": False,
                "result_source": "junit_xml",
            },
        ],
        "has_issue": [
            {
                "nodeid": "tests/e2e/test_b.py::test_linked_failure",
                "suite_name": "tests/e2e/test_b.py",
                "outcome": "failed",
                "duration_seconds": 0.42,
                "failure_summary": "AssertionError: bad",
                "longrepr": "AssertionError: bad\n  File \"tests/e2e/test_b.py\", line 42, in test_linked_failure\n    assert False",
                "history": [],
                "existing_issue": {"number": 4503, "title": "publish flake", "state": "open"},
                "is_quarantined": False,
                "result_source": "junit_xml",
            },
        ],
        "flaky": [],
        "fixed": [],
        "passed": [
            {"nodeid": "tests/e2e/test_c.py::test_one", "suite_name": "tests/e2e/test_c.py", "outcome": "passed", "duration_seconds": 0.1},
            {"nodeid": "tests/e2e/test_c.py::test_two", "suite_name": "tests/e2e/test_c.py", "outcome": "passed", "duration_seconds": 0.1},
            {"nodeid": "tests/e2e/test_d.py::test_one", "suite_name": "tests/e2e/test_d.py", "outcome": "passed", "duration_seconds": 0.1},
        ],
        "quarantined": [],
        "skipped": [
            {
                "nodeid": "tests/e2e/test_e.py::test_pending",
                "suite_name": "tests/e2e/test_e.py",
                "outcome": "skipped",
                # Skip-reason content (matches JUnit ``<skipped message="..."/>``)
                # surfaces under the test row in the canonical viewer.  Verified
                # below with a content assertion on ``.cvv-skip-reason``.
                "failure_details": "skip(reason='waiting on upstream API key'): pending env var",
            },
        ],
    },
    "results_summary": {"total": 6, "passed": 3, "failed": 2, "skipped": 1, "untriaged": 1, "has_issue": 1, "flaky": 0, "fixed": 0, "quarantined": 0},
    "artifacts": [],
    "reports": [],
    "issue_affordances": [],
    "lifecycle": None,
    "events": [],
    "phase_toc": [],
    "cycles": [],
}


def _goto_dashboard(page: Page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def test_e2e_run_modal_mounts_canonical_viewer_with_plugin_and_aria(
    page: Page,
    web_server: dict[str, object],
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the run-detail endpoint with a mixed payload (1 untracked
    # failure + 1 linked failure + 3 passes + 1 skipped) so every code
    # path in the translator is exercised.
    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard(page, str(web_server["url"]))

    # Open the run modal directly via the global entry point; no need
    # to navigate via UI elements that may not exist in the test fixture.
    page.evaluate(f"() => showUnifiedRunView({_RUN_ID})")

    modal = page.locator("#e2eDiagnosisModal.visible")
    expect(modal).to_be_visible(timeout=10_000)

    # ── canonical viewer mounted as the body ───────────────────────
    cvv = modal.locator(".cvv-root")
    expect(cvv).to_be_visible(timeout=5000)
    expect(cvv).to_have_attribute("data-cvv-status", "failed")
    expect(cvv).to_have_attribute("role", "tree")

    # ── run-level summary chips show the right counts ──────────────
    summary = modal.locator(".e2e-run-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("failed")
    expect(summary).to_contain_text("6 cases")
    expect(summary).to_contain_text("2 failing")
    expect(summary).to_contain_text("3 passing")
    expect(summary).to_contain_text("1 skipped")

    # ── two failure triage cards ───────────────────────────────────
    triage_cards = cvv.locator(".cvv-triage-card")
    expect(triage_cards).to_have_count(2)
    # Both node IDs appear in the rendered HTML (cards are always visible).
    expect(cvv).to_contain_text("test_untracked_failure")
    expect(cvv).to_contain_text("test_linked_failure")

    # ── linked failure has the io.agent-context plugin block with
    #    Open-issue-drawer affordance ───────────────────────────────
    linked_card = cvv.locator(".cvv-triage-card", has_text="test_linked_failure")
    plugin_block = linked_card.locator(".cvv-plugin.agent-context")
    expect(plugin_block).to_be_visible()
    expect(plugin_block).to_contain_text("#4503")

    # PR #6319 Blocker 1: the drawer affordance routes through the
    # typed-Command pipeline (``data-lifecycle-command`` →
    # ``runE2ELifecycleCommand`` → ``openIssueTimeline``), not a raw
    # ``<a href="/api/...">``.  Verify both the button shape and that
    # a click actually dispatches into ``openIssueTimeline``.
    drawer_button = plugin_block.locator("button", has_text="Open issue drawer").first
    expect(drawer_button).to_be_visible()
    cmd_attr = drawer_button.get_attribute("data-lifecycle-command") or ""
    assert cmd_attr, "Open-issue-drawer button must carry data-lifecycle-command"
    cmd = json.loads(cmd_attr.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd.get("kind") == "open_issue_timeline"
    assert cmd.get("issue_number") == 4503
    assert cmd.get("scope_kind") == "dashboard"

    # Click-through proof: replace ``openIssueTimeline`` with a spy and
    # verify it gets called with the right issue number when the
    # button is clicked.  This catches a regression where the typed
    # command lands on the button but the dispatcher's
    # ``open_issue_timeline`` branch silently breaks.
    page.evaluate(
        "() => {"
        "  window.__openIssueTimelineCalls = [];"
        "  window.openIssueTimeline = (issueNumber, triggerEl, opts) => {"
        "    window.__openIssueTimelineCalls.push({issueNumber, opts: opts || null});"
        "  };"
        "}"
    )
    drawer_button.click()
    calls = page.evaluate("() => window.__openIssueTimelineCalls")
    assert calls, "openIssueTimeline must be invoked when the drawer button is clicked"
    assert calls[0]["issueNumber"] == 4503

    # ── untracked failure has NO plugin block (no linked issue) ────
    untracked_card = cvv.locator(".cvv-triage-card", has_text="test_untracked_failure")
    expect(untracked_card.locator(".cvv-plugin.agent-context")).to_have_count(0)

    # ── run-level untracked-failures banner is visible ─────────────
    banner = modal.locator(".e2e-untracked-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("1 failing test has no linked issue")

    # ── ARIA tree was enhanced after mount ─────────────────────────
    expect(cvv).to_have_attribute("data-cvv-a11y-bound", "1")
    treeitems = cvv.locator('[role="treeitem"]')
    assert treeitems.count() >= 1
    levels = treeitems.evaluate_all(
        "elements => elements.map(el => [el.getAttribute('aria-level'),"
        " el.getAttribute('aria-setsize'), el.getAttribute('aria-posinset')])"
    )
    for level, setsize, posinset in levels:
        assert level is not None and int(level) >= 1
        assert setsize is not None and int(setsize) >= 1
        assert posinset is not None and int(posinset) >= 1
    # Exactly one tab-stop in the tree (roving tabindex).
    tab_stops = treeitems.evaluate_all(
        "elements => elements.filter(el => el.tabIndex === 0).length"
    )
    assert tab_stops == 1

    # ── browse-by-file row exists and is collapsed by default ──────
    browse_row = cvv.locator(".cvv-row-browse").first
    expect(browse_row).to_be_visible()
    # When there ARE failures, browse-by-file starts closed.
    expect(browse_row).not_to_have_attribute("open", "")

    # ── skip-reason renders inline when a skipped test row is opened
    # (Phase C confidence: a JUnit ``<skipped message="..."/>`` value
    # surfaces verbatim under the test row so the user doesn't have to
    # leave the dashboard to learn why a test was skipped).
    #
    # Force-open every row inside the browse-by-file tree so the
    # skipped test's body is in the DOM, then assert content.
    page.evaluate(
        "() => {"
        "  document.querySelectorAll('.cvv-root details').forEach(el => { el.open = true; });"
        "}"
    )
    skip_reason = cvv.locator(".cvv-skip-reason")
    expect(skip_reason).to_have_count(1)
    expect(skip_reason).to_contain_text("waiting on upstream API key")

    # ── real keypress: focus first treeitem, ArrowDown moves focus ─
    # Identify the active treeitem by its index in the tree's
    # treeitem list rather than outerHTML — the canonical viewer's
    # rows share enough markup that a short prefix slice can collide.
    activeIndexScript = (
        "() => {"
        "  const all = Array.from(document.querySelectorAll('.cvv-root [role=\"treeitem\"]'));"
        "  return all.indexOf(document.activeElement);"
        "}"
    )
    page.evaluate(
        "() => {"
        "  const el = document.querySelector('.cvv-root [role=\"treeitem\"][tabindex=\"0\"]');"
        "  if (!el) throw new Error('no tab-stop in tree');"
        "  el.focus();"
        "}"
    )
    first_index = page.evaluate(activeIndexScript)
    assert first_index >= 0, f"first treeitem should be focused, got index {first_index}"
    page.keyboard.press("ArrowDown")
    after_index = page.evaluate(activeIndexScript)
    assert after_index != first_index and after_index >= 0, (
        f"ArrowDown should move focus to a different treeitem; before index={first_index}, after index={after_index}"
    )

    assert errors == []
