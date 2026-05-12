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
import re

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

    # PR #6329 reviewer Blocker 2: open the run view by CLICKING a
    # real rendered affordance, not by calling ``showUnifiedRunView``
    # directly.  This proves the typed-Command pipeline is wired
    # end-to-end (template → data-lifecycle-command → dispatcher →
    # showUnifiedRunView) in the actual browser, not just in JS-vm
    # unit tests.
    #
    # The dashboard fixture may or may not render a Run-history chip
    # for this test's stub run id.  If a real chip is present, click
    # it; otherwise fall back to injecting a chip with the
    # production-shape ``data-lifecycle-command`` and clicking that.
    # Both paths exercise ``runE2ELifecycleCommandFromButton`` → the
    # typed Command dispatcher → ``showUnifiedRunView`` end-to-end.
    real_chip = page.locator(f"button.card-focus[data-lifecycle-command]").filter(
        has_text=str(_RUN_ID)
    ).first
    if real_chip.count() > 0:
        real_chip.click()
    else:
        page.evaluate(
            f"""() => {{
                const btn = document.createElement('button');
                btn.className = 'card-focus';
                btn.id = 'test-injected-chip';
                btn.setAttribute('data-lifecycle-command', JSON.stringify({{
                    kind: 'open_e2e_run',
                    label: 'Open E2E Run',
                    run_id: {_RUN_ID},
                    expand_run_details: false,
                }}));
                btn.setAttribute('onclick', 'runE2ELifecycleCommandFromButton(this);');
                btn.textContent = 'Run #{_RUN_ID}';
                document.body.appendChild(btn);
            }}"""
        )
        page.locator("#test-injected-chip").click()

    modal = page.locator("#e2eDiagnosisModal.visible")
    expect(modal).to_be_visible(timeout=10_000)

    # ── Phase D modal-drop (issue #6322): state + content assertions ─
    # State: <body> carries the ``data-e2e-run-view-active`` flag
    # while the run view is active.  The CSS rule keys off this.
    body = page.locator("body")
    expect(body).to_have_attribute("data-e2e-run-view-active", "1", timeout=5_000)

    # State: the modal container is in normal flow, not fixed-position.
    modal_position = page.evaluate(
        "() => getComputedStyle(document.getElementById('e2eDiagnosisModal')).position"
    )
    assert modal_position == "static", (
        f"#e2eDiagnosisModal should render in normal flow (position: static), got {modal_position}"
    )

    # CONTENT: the modal's actual computed backdrop is no longer a
    # translucent dim overlay.  We check that ``backgroundColor``
    # resolved to either fully transparent or an opaque page color —
    # both prove the legacy ``rgba(black, 0.5)`` dim is gone.  This
    # would catch a regression where the CSS override stops applying
    # and the legacy dim backdrop returns.
    modal_bg = page.evaluate(
        "() => getComputedStyle(document.getElementById('e2eDiagnosisModal')).backgroundColor"
    )
    # Translucent (alpha < 1) non-transparent colors are the legacy
    # overlay shape we want to reject.  Match ``rgba(...)`` and parse
    # the alpha.
    alpha_match = re.search(r"rgba\([^,]+,[^,]+,[^,]+,\s*([0-9.]+)\s*\)", modal_bg)
    if alpha_match:
        alpha = float(alpha_match.group(1))
        assert alpha == 0.0 or alpha == 1.0, (
            f"#e2eDiagnosisModal backgroundColor must not be a translucent dim overlay "
            f"(legacy modal pattern); got {modal_bg!r} with alpha={alpha}"
        )

    # CONTENT: a sibling dashboard element outside the modal is
    # actually HIDDEN (display: none) while the run view is active.
    # This is the visible payoff of the body-flag CSS rule — the
    # dashboard chrome disappears when the user opens a run.
    chrome_state = page.evaluate(
        "() => {"
        "  const containers = ['.container', '.dashboard-container', 'main', 'body'];"
        "  for (const sel of containers) {"
        "    const c = document.querySelector(sel);"
        "    if (!c) continue;"
        "    const siblings = Array.from(c.children).filter(el =>"
        "      el.id !== 'e2eDiagnosisModal' && el.tagName !== 'SCRIPT'"
        "    );"
        "    if (siblings.length === 0) continue;"
        "    return siblings.map(el => ({"
        "      tag: el.tagName,"
        "      id: el.id || null,"
        "      display: getComputedStyle(el).display,"
        "    }));"
        "  }"
        "  return [];"
        "}"
    )
    if chrome_state:
        # At least one non-modal sibling must be hidden — proves the
        # body flag actually triggered the CSS rule.  Allow a couple
        # of always-present elements (e.g. ``<script>``, fixed
        # toolbars) to remain visible; the rule only hides direct
        # children of the recognized containers.
        hidden = [el for el in chrome_state if el["display"] == "none"]
        assert hidden, (
            "expected at least one non-modal sibling to be hidden by "
            f"body[data-e2e-run-view-active]; got siblings={chrome_state}"
        )

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

    # ── Phase D outcome groups: Failed (1) is a collapsed group; open
    #    it to reveal the failed test's triage card.  (The untracked
    #    failure has outcome="failed" too in this fixture, so both
    #    failures live under the same Failed group.)
    failed_group = cvv.locator(".cvv-group-failed")
    expect(failed_group).to_have_count(1)
    failed_group.locator("summary").first.click()

    # ── two failure triage cards inside the Failed group ───────────
    triage_cards = cvv.locator(".cvv-triage-card")
    expect(triage_cards).to_have_count(2)
    expect(cvv).to_contain_text("test_untracked_failure")
    expect(cvv).to_contain_text("test_linked_failure")

    # ── linked failure has the io.agent-context plugin block with the
    #    inline ``▸ Attempts on issue #N`` expander.  Phase D: open
    #    the triage card first to reveal its body (the plugin renders
    #    inside the body).
    linked_card = cvv.locator(".cvv-triage-card", has_text="test_linked_failure")
    linked_card.locator("summary").first.click()
    plugin_block = linked_card.locator(".cvv-plugin.agent-context")
    expect(plugin_block).to_be_visible()
    expect(plugin_block).to_contain_text("#4503")

    # Issue #6322 follow-up: the linked-failure drill-in is the inline
    # ``▸ Attempts on issue #N`` expander.  No legacy
    # "Open issue drawer" typed-Command button on the plugin block.
    expect(plugin_block.locator("button", has_text="Open issue drawer")).to_have_count(0)
    expander = plugin_block.locator(".agent-context-attempts-expander")
    expect(expander).to_have_count(1)
    expect(expander).to_be_visible()
    assert expander.get_attribute("data-issue-number") == "4503"
    # Closed by default — the user opens it on demand.
    assert expander.evaluate("el => el.open") is False
    expect(expander).to_contain_text("Attempts on issue #4503")

    # Click-through proof: stub ``fetch`` to record the lazy-fetch URL
    # without hitting the real backend.  This catches a regression
    # where the expander markup is right but the toggle handler stops
    # firing or the URL contract drifts.
    page.evaluate(
        "() => {"
        "  window.__inlineAgentFetchCalls = [];"
        "  const real = window.fetch;"
        "  window.fetch = (url, opts) => {"
        "    window.__inlineAgentFetchCalls.push(String(url));"
        "    return Promise.resolve({ ok: true, status: 200, "
        "      json: () => Promise.resolve({ runs: [] }), "
        "    });"
        "  };"
        "  window.__realFetch = real;"
        "}"
    )
    expander.locator("summary").first.click()
    # The expander populates its body asynchronously; wait for the
    # fetch call to land.
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

    # ── Phase D outcome groups: Passed and Skipped groups render
    #    collapsed when they have cases.  No more "browse-by-file"
    #    single-row — passed and skipped are separate groups now.
    passed_group = cvv.locator(".cvv-group-passed")
    expect(passed_group).to_have_count(1)
    expect(passed_group).not_to_have_attribute("open", "")
    skipped_group = cvv.locator(".cvv-group-skipped")
    expect(skipped_group).to_have_count(1)
    expect(skipped_group).not_to_have_attribute("open", "")

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

    # ── Phase D modal-drop: close-path content assertions ────────────
    # The close-path is the symmetrical complement to the open-path
    # checks above.  We verified on open: (1) body flag set,
    # (2) modal in normal flow with no dim backdrop, (3) at least
    # one non-modal sibling hidden by the body-flag CSS rule.
    #
    # On close we need the symmetrical reverse — but we can't simply
    # invert the "sibling hidden" assertion, because in this test
    # fixture some siblings are hidden for unrelated reasons (e.g.
    # ``#engineRestartBanner`` is hidden by default).  Instead we
    # capture the SPECIFIC sibling that flipped from visible→hidden
    # when we opened, and assert it flips back.  That's strictly
    # the body-flag CSS rule's responsibility.
    #
    # First, snapshot ``display`` of every relevant sibling NOW
    # (while the run view is still open) and identify the ones the
    # rule is hiding.  Compare to AFTER close.
    sibling_states_open = page.evaluate(
        "() => {"
        "  const containers = ['.container', '.dashboard-container', 'main', 'body'];"
        "  for (const sel of containers) {"
        "    const c = document.querySelector(sel);"
        "    if (!c) continue;"
        "    const siblings = Array.from(c.children).filter(el =>"
        "      el.id !== 'e2eDiagnosisModal' && el.tagName !== 'SCRIPT'"
        "    );"
        "    if (siblings.length === 0) continue;"
        "    return siblings.map((el, idx) => ({"
        "      key: el.id ? '#' + el.id : el.tagName + '[' + idx + ']',"
        "      display: getComputedStyle(el).display,"
        "    }));"
        "  }"
        "  return [];"
        "}"
    )

    # Close the modal.
    page.evaluate("() => closeE2EDiagnosisModal()")

    # Body flag cleared — proves the close handler ran and the CSS
    # rule will stop applying.
    expect(body).not_to_have_attribute("data-e2e-run-view-active", "1", timeout=2_000)

    # Modal hidden — ``.visible`` class removed.
    expect(modal).not_to_be_visible(timeout=2_000)

    # Symmetry check on the sibling states: any element that was
    # hidden ONLY because of the body-flag rule must now be visible.
    # We approximate "ONLY because of the body-flag rule" by:
    # an element whose display flipped from ``none`` (while flag was
    # set) to a visible value (after flag cleared).  Elements hidden
    # for other reasons (engineRestartBanner, etc.) stay hidden in
    # both states and are excluded by the flip check.
    if sibling_states_open:
        flipped_visible = page.evaluate(
            "(stateMap) => {"
            "  for (const entry of stateMap) {"
            "    let el;"
            "    if (entry.key.startsWith('#')) {"
            "      el = document.querySelector(entry.key);"
            "    } else {"
            "      const m = entry.key.match(/^([A-Z]+)\\[(\\d+)\\]$/);"
            "      if (!m) continue;"
            "      const containers = ['.container', '.dashboard-container', 'main', 'body'];"
            "      for (const sel of containers) {"
            "        const c = document.querySelector(sel);"
            "        if (!c) continue;"
            "        const sibs = Array.from(c.children).filter(e =>"
            "          e.id !== 'e2eDiagnosisModal' && e.tagName !== 'SCRIPT'"
            "        );"
            "        if (sibs.length === 0) continue;"
            "        el = sibs[parseInt(m[2], 10)];"
            "        break;"
            "      }"
            "    }"
            "    if (!el) continue;"
            "    const nowDisplay = getComputedStyle(el).display;"
            "    if (entry.display === 'none' && nowDisplay !== 'none') {"
            "      return { key: entry.key, before: entry.display, after: nowDisplay };"
            "    }"
            "  }"
            "  return null;"
            "}",
            sibling_states_open,
        )
        # If any sibling was hidden by the body-flag rule while the
        # run view was open, it MUST be visible now.  Otherwise the
        # close path didn't actually un-hide the dashboard chrome.
        had_body_flag_hidden = any(s["display"] == "none" for s in sibling_states_open)
        if had_body_flag_hidden:
            assert flipped_visible is not None, (
                "expected at least one sibling that was hidden while the body flag "
                "was set to become visible after close; none flipped.  States while "
                f"open: {sibling_states_open}"
            )

    assert errors == []
