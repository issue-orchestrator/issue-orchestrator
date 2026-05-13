"""Playwright smoke for the inline E2E runs-as-rows list (issue #6334).

Pins three things in the live pipeline (template → JS chunk → typed
Command → dispatcher → lazy fetch → canonical viewer mount):

  1. The Run History panel renders one ``<details class="e2e-run-row">``
     per recent E2E run, each carrying a typed ``expand_e2e_run``
     Command in ``data-lifecycle-command`` and the shared
     ``runE2ELifecycleCommandFromToggle`` dispatcher hook.
  2. Clicking a row's summary triggers a lazy fetch of
     ``/api/e2e-run-detail/{run_id}`` and mounts the canonical
     viewer (``.cvv-root``) inside the row body — no modal teleport.
  3. ``#e2eDiagnosisModal`` is NOT in the DOM at all.

The matrix of per-tone rendering, dispatcher branches, and predictable-
collapse is covered by ``tests/js/e2e_runs_list.test.js``; this is the
end-to-end live-pipeline proof.
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, expect


_RUN_ID = 7777


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
        "has_issue": [],
        "flaky": [],
        "fixed": [],
        "passed": [
            {"nodeid": "tests/e2e/test_c.py::test_one", "suite_name": "tests/e2e/test_c.py", "outcome": "passed", "duration_seconds": 0.1},
        ],
        "quarantined": [],
        "skipped": [],
    },
    "results_summary": {"total": 2, "passed": 1, "failed": 1, "skipped": 0, "untriaged": 1, "has_issue": 0, "flaky": 0, "fixed": 0, "quarantined": 0},
    "artifacts": [],
    "reports": [],
    "issue_affordances": [],
    "lifecycle": None,
    "events": [],
    "phase_toc": [],
    "cycles": [],
}


_STUB_RECENT_RUNS_PAYLOAD: dict[str, object] = {
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
                "passed": 1, "failed": 1, "errored": 0,
                "skipped": 0, "quarantined": 0, "total": 2,
            },
            "note": None,
            "expand_command": {
                "kind": "expand_e2e_run",
                "label": "Expand E2E Run",
                "run_id": _RUN_ID,
            },
        },
        {
            "run_id": _RUN_ID + 1,
            "outcome": {"label": "Passed", "tone": "passed"},
            "started_at": "2026-05-11T01:00:00Z",
            "finished_at": "2026-05-11T01:00:30Z",
            "duration_seconds": 30.0,
            "commit_sha": "deadbeef",
            "branch": "main",
            "runner_kind": "pytest",
            "command_summary": "pytest tests/e2e",
            "results": {
                "passed": 36, "failed": 0, "errored": 0,
                "skipped": 0, "quarantined": 0, "total": 36,
            },
            "note": None,
            "expand_command": {
                "kind": "expand_e2e_run",
                "label": "Expand E2E Run",
                "run_id": _RUN_ID + 1,
            },
        },
    ],
}


def _goto_dashboard_e2e_tab(page: Page, base_url: str) -> None:
    # The runs-list mount point lives inside the E2E panel; navigate
    # straight to ``?tab=e2e`` so the runs list root is in the DOM
    # on first paint.
    page.goto(f"{base_url}/?tab=e2e", wait_until="domcontentloaded", timeout=90_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)


def test_inline_runs_list_renders_rows_and_mounts_canonical_viewer_on_expand(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Click a row, prove the canonical viewer mounts inline (#6334).

    Asserts the issue body's Playwright-smoke spec:
      * N rows render
      * click one row's summary
      * the canonical viewer mounts inline with the correct run id
      * ``#e2eDiagnosisModal`` is not in the DOM at all
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    # Stub the per-run detail endpoint so the lazy-fetch on expand
    # resolves to a deterministic fixture without hitting the real
    # backend.
    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    # The default ``web_server`` fixture's mock orchestrator has E2E
    # disabled, so the E2E panel (and the runs-list root) aren't in
    # the SSR HTML.  Inject the mount points + stubbed payload, then
    # call the renderer directly — the production code path is
    # exactly this (``renderE2ERunsList`` reads typed payload, mounts
    # into ``#e2eRunsListRoot``).
    page.evaluate(
        f"""(payload) => {{
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {{
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }}
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }}""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )

    # The modal is gone — assert it's not in the DOM at all (the
    # issue body's explicit guardrail).  Note: ``to_have_count(0)``
    # passes for "element not found" so this is the sticky way to
    # prove the modal markup is dropped.
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    # ── 1. N rows render ─────────────────────────────────────────────
    rows = page.locator("details.e2e-run-row")
    expect(rows).to_have_count(2, timeout=10_000)
    # Each row carries the typed Command in data-lifecycle-command +
    # the shared toggle dispatcher (single-owner contract).
    first_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    expect(first_row).to_have_count(1)
    cmd_raw = first_row.get_attribute("data-lifecycle-command") or ""
    assert cmd_raw, "row must carry data-lifecycle-command"
    cmd = json.loads(cmd_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd == {
        "kind": "expand_e2e_run",
        "label": "Expand E2E Run",
        "run_id": _RUN_ID,
    }, f"unexpected typed Command on row: {cmd!r}"
    ontoggle = first_row.get_attribute("ontoggle") or ""
    assert "runE2ELifecycleCommandFromToggle" in ontoggle, (
        f"row ontoggle must route through the shared dispatcher; got {ontoggle!r}"
    )

    # Closed by default — predictable-collapse rule from issue #6322.
    assert first_row.evaluate("el => el.open") is False

    # ── 2. Click the row's summary to expand it ──────────────────────
    first_row.locator("summary").click()
    expect(first_row).to_have_js_property("open", True)

    # ── 3. Canonical viewer mounts inline with the right run id ──────
    # ``loadE2ERunIntoRow`` fetches ``/api/e2e-run-detail/{run_id}`` and
    # mounts ``renderE2EResultsPanel(data)`` inside ``.e2e-run-row-content``.
    cvv = first_row.locator(".cvv-root")
    expect(cvv).to_be_visible(timeout=10_000)
    expect(cvv).to_have_attribute("data-cvv-status", "failed")

    # Run-level summary chips show the right counts from the lazy
    # fetch — proves we mounted THIS run's data, not a different one.
    summary = first_row.locator(".e2e-run-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("failed")
    expect(summary).to_contain_text("2 cases")
    expect(summary).to_contain_text("1 failing")
    expect(summary).to_contain_text("1 passing")

    # ── 4. Re-collapsing the row keeps the cache (predictable-collapse) ─
    # ``data-loaded`` flips to '1' on first open; closing + reopening
    # must not re-fetch.  We can't observe the network easily here,
    # so test the marker.
    assert first_row.evaluate("el => el.dataset.loaded") == "1"

    # ── 5. The other row is still closed and has its own typed Command ─
    second_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID + 1}']")
    assert second_row.evaluate("el => el.open") is False
    cmd2_raw = second_row.get_attribute("data-lifecycle-command") or ""
    cmd2 = json.loads(cmd2_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd2["run_id"] == _RUN_ID + 1

    # No page errors during the run.
    assert not errors, f"unexpected page errors: {errors}"


def test_two_rows_expanded_act_independently(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Issue #6334 round-2 reviewer blocker: two rows expanded at the
    same time must NOT share state.

    The legacy ``unifiedRunData`` module-level singleton broke as
    soon as a second row could be expanded.  The fix moved
    ownership to the row itself (``row._e2eRunData``) and emits
    typed Commands (``switch_e2e_timeline_view``,
    ``create_e2e_untriaged_issues``) that carry the row's
    ``run_id`` explicitly.

    This live-browser smoke verifies:

      1. Row A and Row B both load their OWN detail payload (no
         shared state).
      2. Clicking a Story/Ops/Debug button inside Row A fetches
         row A's run id with the chosen view, leaves Row B's
         timeline untouched.
      3. The two rows' "Run details & artifacts" disclosures and
         timeline containers are independent — no document-global
         id collisions.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_a_id = _RUN_ID
    run_b_id = _RUN_ID + 1
    # Per-run detail payloads — distinct enough to detect cross-row
    # contamination if it happens.
    detail_a = {**_STUB_RUN_DETAIL, "run": {**_STUB_RUN_DETAIL["run"], "id": run_a_id}}
    detail_b = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_b_id, "branch": "feature-b"},
    }

    # Track every detail fetch so the test can assert on which run
    # ids + which views were requested.
    fetched: list[str] = []

    def _route_detail(detail_payload: dict[str, object], expected_id: int):
        def handler(route):
            fetched.append(route.request.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(detail_payload),
            )
        return handler

    page.route(f"**/api/e2e-run-detail/{run_a_id}**", _route_detail(detail_a, run_a_id))
    page.route(f"**/api/e2e-run-detail/{run_b_id}**", _route_detail(detail_b, run_b_id))

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    page.evaluate(
        f"""(payload) => {{
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {{
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }}
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }}""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )

    row_a = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_a_id}']")
    row_b = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_b_id}']")
    expect(row_a).to_have_count(1)
    expect(row_b).to_have_count(1)

    # ── 1. Expand both rows ─────────────────────────────────────────
    row_a.locator("summary").first.click()
    expect(row_a.locator(".cvv-root")).to_be_visible(timeout=10_000)
    row_b.locator("summary").first.click()
    expect(row_b.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # Each row got its OWN detail fetch.
    assert any(f"/api/e2e-run-detail/{run_a_id}" in url for url in fetched), \
        f"row A detail not fetched; saw: {fetched}"
    assert any(f"/api/e2e-run-detail/{run_b_id}" in url for url in fetched), \
        f"row B detail not fetched; saw: {fetched}"

    # ── 2. Two distinct "Run details & artifacts" disclosures ──────
    # Each row has ONE — not the SSR-collision case of two
    # ``#runDetailsDisclosure`` ids in the DOM.
    expect(row_a.locator(".run-details-disclosure")).to_have_count(1)
    expect(row_b.locator(".run-details-disclosure")).to_have_count(1)
    # Open the disclosure inside each row.
    row_a.locator(".run-details-disclosure summary").first.click()
    row_b.locator(".run-details-disclosure summary").first.click()
    expect(row_a.locator(".e2e-timeline-content")).to_be_visible(timeout=5_000)
    expect(row_b.locator(".e2e-timeline-content")).to_be_visible(timeout=5_000)

    # ── 3. Each row's view-switcher Story/Ops/Debug button carries
    # a typed Command pinned to THAT row's run id ────────────────
    btn_a_ops = row_a.locator(".e2e-view-btn[data-view='ops']").first
    cmd_a = json.loads(
        (btn_a_ops.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert cmd_a == {
        "kind": "switch_e2e_timeline_view",
        "label": "Switch suite timeline to Ops",
        "run_id": run_a_id,
        "view": "ops",
    }, f"row A Ops button has wrong typed Command: {cmd_a!r}"

    btn_b_debug = row_b.locator(".e2e-view-btn[data-view='debug']").first
    cmd_b = json.loads(
        (btn_b_debug.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert cmd_b["run_id"] == run_b_id, \
        f"row B Debug button must carry run_id={run_b_id}, got {cmd_b!r}"
    assert cmd_b["view"] == "debug"

    # ── 4. Click Row A's Ops button — only Row A's timeline refetches ─
    fetched.clear()
    with page.expect_request(
        lambda req: f"/api/e2e-run-detail/{run_a_id}" in req.url and "view=ops" in req.url,
        timeout=5_000,
    ):
        btn_a_ops.click()
    # The fetch went to row A's run id with view=ops.
    a_ops_fetches = [u for u in fetched if f"/api/e2e-run-detail/{run_a_id}" in u and "view=ops" in u]
    assert a_ops_fetches, f"row A Ops click did not fetch row A detail; saw: {fetched}"
    # Row B did NOT refetch.
    assert not any(f"/api/e2e-run-detail/{run_b_id}" in u for u in fetched), \
        f"row B timeline must NOT refetch when row A switches view; saw: {fetched}"

    # ── 5. The ``active`` class moved on row A's switcher only ──────
    expect(row_a.locator(".e2e-view-btn[data-view='ops']")).to_have_class(
        "e2e-view-btn active"
    )
    # Row B's Story remains active (untouched).
    expect(row_b.locator(".e2e-view-btn[data-view='user']")).to_have_class(
        "e2e-view-btn active"
    )

    # ── 6. The untracked-failures banners carry typed
    # ``create_e2e_untriaged_issues`` Commands pinned to each row's
    # run id (so a click in row A targets only row A's untriaged
    # tests). ─────────────────────────────────────────────────────
    banner_a_btn = row_a.locator(".e2e-untracked-banner button.btn-primary").first
    create_a = json.loads(
        (banner_a_btn.get_attribute("data-lifecycle-command") or "")
        .replace("&quot;", '"').replace("&amp;", "&")
    )
    assert create_a["kind"] == "create_e2e_untriaged_issues"
    assert create_a["run_id"] == run_a_id
    # Each row has its OWN agent select — no document-global ``#unifiedRunAgent``.
    expect(row_a.locator(".unified-run-agent")).to_have_count(1)
    expect(row_b.locator(".unified-run-agent")).to_have_count(1)
    expect(page.locator("#unifiedRunAgent")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"


def test_open_e2e_run_command_reroutes_to_row_expansion_not_modal(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """Issue #6334: ``open_e2e_run`` dispatches to ``expandE2ERunRow``,
    which opens the matching row inline — the dropped
    ``showUnifiedRunView`` modal driver is no longer in the dispatcher.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.route(
        f"**/api/e2e-run-detail/{_RUN_ID}**",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_STUB_RUN_DETAIL),
        ),
    )

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    # Inject the mount point + render the runs list (see explanation
    # in the inline-mount test above).
    page.evaluate(
        f"""(payload) => {{
            const container = document.querySelector('#panel-e2e')
                || document.querySelector('main')
                || document.body;
            if (!document.getElementById('e2eRunsListRoot')) {{
                const root = document.createElement('div');
                root.id = 'e2eRunsListRoot';
                container.appendChild(root);
            }}
            const root = document.getElementById('e2eRunsListRoot');
            root.innerHTML = window.renderE2ERunsList(payload);
        }}""",
        _STUB_RECENT_RUNS_PAYLOAD,
    )

    # Dispatch the typed ``open_e2e_run`` Command directly via the
    # global dispatcher — same path the chip / View button take.
    page.evaluate(
        f"""() => window.runE2ELifecycleCommand({{
            kind: 'open_e2e_run',
            label: 'Open E2E Run',
            run_id: {_RUN_ID},
            expand_run_details: false,
        }})"""
    )

    # The matching row opens (and scrolls into view).
    target_row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{_RUN_ID}']")
    expect(target_row).to_have_js_property("open", True)
    # Canonical viewer mounted via the same lazy-fetch path.
    expect(target_row.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # No modal popped open — the dropped ``#e2eDiagnosisModal`` is
    # not in the DOM at all.
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"
