"""Phase C of #6310 follow-up: Playwright smoke for the canonical viewer
mount on an E2E run.

Issue #6334 retired the ``#e2eDiagnosisModal`` overlay; the canonical
viewer now mounts inline inside the run's ``<details>`` row in the
inline runs-as-rows list.  The test pins the same end-to-end pipeline
it always did, just inside the row body:

- The runs list renders ``<details class="e2e-run-row">`` rows from
  the typed ``RecentE2ERunsPayload`` (no modal).
- Clicking a row's summary lazy-fetches ``/api/e2e-run-detail/{run_id}``
  and mounts ``.cvv-root`` inside ``.e2e-run-row-content``.
- Run-level summary chips show the right counts.
- Failed tests render as triage cards; failures with a linked issue
  carry the ``io.agent-context`` plugin block beneath them.
- The canonical viewer is ARIA-enhanced after mount
  (``role="tree"``, every ``role="treeitem"`` has ``aria-level``,
  ``aria-setsize``, and ``aria-posinset``).
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


_STUB_RECENT_RUNS: dict[str, object] = {
    "runs": [
        {
            "run_id": _RUN_ID,
            "outcome": {"label": "Failed", "tone": "failed"},
            "started_at": "2026-05-12T01:00:00Z",
            "finished_at": "2026-05-12T01:01:00Z",
            "duration_seconds": 60.0,
            "commit_sha": "abc1234",
            "branch": "main",
            "runner_kind": "pytest",
            "command_summary": "pytest tests/e2e --junit-xml=junit.xml",
            "results": {
                "passed": 3, "failed": 2, "errored": 0,
                "skipped": 1, "quarantined": 0, "total": 6,
            },
            "note": None,
            "expand_command": {
                "kind": "expand_e2e_run",
                "label": "Expand E2E Run",
                "run_id": _RUN_ID,
            },
        },
    ],
}


def _goto_dashboard(page: Page, base_url: str) -> None:
    # Navigate straight to the E2E tab so the runs-list root is in
    # the DOM on first paint (the E2E panel is gated by ``active_tab``).
    page.goto(f"{base_url}/?tab=e2e", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def _inject_runs_list(page: Page, payload: dict[str, object]) -> None:
    """Render the runs-list with the given typed payload.

    The default ``web_server`` fixture's mock orchestrator has E2E
    disabled, so the SSR HTML lacks the ``#e2eRunsListRoot`` mount
    point.  This injects the mount point and calls the production
    renderer directly — same code path the JS chunk runs on
    DOMContentLoaded.
    """
    page.evaluate(
        """(payload) => {
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }""",
        payload,
    )


def test_e2e_run_modal_mounts_canonical_viewer_with_plugin_and_aria(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Issue #6322 / #6334: canonical viewer mounts inline in the run row.

    Test name preserved from the modal era for git-blame continuity;
    the assertions now target the inline runs-list row (``#6334``
    dropped ``#e2eDiagnosisModal``).
    """
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
    _inject_runs_list(page, _STUB_RECENT_RUNS)

    # The dropped modal is not in the DOM.
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    # Locate the row by run_id and click its summary to expand.  The
    # row's ``ontoggle`` dispatches through the typed-Command pipeline
    # (``runLifecycleCommandFromToggle`` → ``loadE2ERunIntoRow``).
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    expect(row).to_have_count(1)
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", True)

    # ── canonical viewer mounted as the row body ────────────────────
    cvv = row.locator(".cvv-root")
    expect(cvv).to_be_visible(timeout=10_000)
    expect(cvv).to_have_attribute("data-cvv-status", "failed")
    expect(cvv).to_have_attribute("role", "tree")

    # ── run-level summary chips show the right counts ──────────────
    summary = row.locator(".e2e-run-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("failed")
    expect(summary).to_contain_text("6 cases")
    expect(summary).to_contain_text("2 failing")
    expect(summary).to_contain_text("3 passing")
    expect(summary).to_contain_text("1 skipped")

    # ── outcome-grouped expanders: Failed group has 1 collapsed group ─
    failed_group = cvv.locator(".cvv-group-failed")
    expect(failed_group).to_have_count(1)
    failed_group.locator("summary").first.click()

    triage_cards = cvv.locator(".cvv-triage-card")
    expect(triage_cards).to_have_count(2)
    expect(cvv).to_contain_text("test_untracked_failure")
    expect(cvv).to_contain_text("test_linked_failure")

    # ── linked failure carries the io.agent-context plugin block ────
    linked_card = cvv.locator(".cvv-triage-card", has_text="test_linked_failure")
    linked_card.locator("summary").first.click()
    plugin_block = linked_card.locator(".cvv-plugin.agent-context")
    expect(plugin_block).to_be_visible()
    expect(plugin_block).to_contain_text("#4503")

    # Issue #6322 follow-up: the inline ``▸ Attempts on issue #N``
    # expander is the only drill-in affordance.  No legacy
    # "Open issue drawer" button.
    expect(plugin_block.locator("button", has_text="Open issue drawer")).to_have_count(0)
    expander = plugin_block.locator(".agent-context-attempts-expander")
    expect(expander).to_have_count(1)
    expect(expander).to_be_visible()
    assert expander.get_attribute("data-issue-number") == "4503"
    assert expander.evaluate("el => el.open") is False
    expect(expander).to_contain_text("Attempts on issue #4503")

    cmd_attr = expander.get_attribute("data-lifecycle-command") or ""
    assert cmd_attr, "expander must carry data-lifecycle-command"
    cmd = json.loads(cmd_attr.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd.get("kind") == "open_inline_agent_attempts"
    assert cmd.get("issue_number") == 4503

    # Click-through proof: stub ``fetch`` so we can record the lazy
    # URL the expander hits.
    page.evaluate(
        "() => {"
        "  window.__inlineAgentFetchCalls = [];"
        "  window.fetch = (url) => {"
        "    window.__inlineAgentFetchCalls.push(String(url));"
        "    return Promise.resolve({ ok: true, status: 200, "
        "      json: () => Promise.resolve({ runs: [] }), "
        "    });"
        "  };"
        "}"
    )
    expander.locator("summary").first.click()
    page.wait_for_function(
        "() => (window.__inlineAgentFetchCalls || []).length > 0",
        timeout=5000,
    )
    calls = page.evaluate("() => window.__inlineAgentFetchCalls")
    assert any("/api/issue-detail/4503" in url for url in calls), (
        f"expected lazy fetch of /api/issue-detail/4503, got: {calls}"
    )

    # ── untracked failure has NO plugin block (no linked issue) ────
    untracked_card = cvv.locator(".cvv-triage-card", has_text="test_untracked_failure")
    expect(untracked_card.locator(".cvv-plugin.agent-context")).to_have_count(0)

    # ── run-level untracked-failures banner is visible ─────────────
    banner = row.locator(".e2e-untracked-banner")
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

    # ── Passed and Skipped groups render collapsed ─────────────────
    passed_group = cvv.locator(".cvv-group-passed")
    expect(passed_group).to_have_count(1)
    expect(passed_group).not_to_have_attribute("open", "")
    skipped_group = cvv.locator(".cvv-group-skipped")
    expect(skipped_group).to_have_count(1)
    expect(skipped_group).not_to_have_attribute("open", "")

    # ── skip-reason renders inline when a skipped test row is opened ─
    page.evaluate(
        "() => {"
        "  document.querySelectorAll('.cvv-root details').forEach(el => { el.open = true; });"
        "}"
    )
    skip_reason = cvv.locator(".cvv-skip-reason")
    expect(skip_reason).to_have_count(1)
    expect(skip_reason).to_contain_text("waiting on upstream API key")

    # ── real keypress: focus first treeitem, ArrowDown moves focus ─
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

    # ── close-path: collapsing the row hides the viewer (no modal cloak) ─
    # Issue #6334 retired the body[data-e2e-run-view-active] cloak; the
    # row simply closes via standard <details> semantics.  The viewer
    # body remains in the DOM (lazy-load cache for re-expansion) but
    # is no longer visible.
    row.locator("summary").first.click()
    expect(row).to_have_js_property("open", False)

    assert not errors, f"unexpected page errors: {errors}"
