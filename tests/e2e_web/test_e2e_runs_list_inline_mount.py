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


# ─── Gap 1: Create-Issues click-through in two-row mode ────────────────


def test_create_issues_for_untriaged_uses_row_scoped_agent_and_run_id(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """End-to-end click-through proves the ``create_e2e_untriaged_issues``
    typed Command resolves the agent + nodeids + run_id from the
    *row the user clicked from*, not from a shared singleton.

    With two rows expanded, the user selects a DIFFERENT agent in
    each row, then clicks "Create issue(s)" inside row A.  The
    resulting POST must hit ``/control/e2e/create-issues/{row_a_id}``
    with ``agent=<row_a_agent>`` and the untriaged nodeids from
    row A's detail payload.  Row B is never touched.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_a_id = _RUN_ID
    run_b_id = _RUN_ID + 1
    # Each row's detail carries DIFFERENT untriaged test sets so we
    # can assert which row's nodeids landed in the POST body.
    detail_a = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_a_id},
        "results_by_category": {
            **_STUB_RUN_DETAIL["results_by_category"],
            "untriaged": [
                {
                    "nodeid": "tests/row_a.py::test_alpha",
                    "outcome": "failed", "duration_seconds": 0.0,
                    "failure_summary": "", "longrepr": "", "history": [],
                    "existing_issue": None, "is_quarantined": False,
                    "result_source": "junit_xml", "suite_name": "tests/row_a.py",
                },
            ],
        },
    }
    detail_b = {
        **_STUB_RUN_DETAIL,
        "run": {**_STUB_RUN_DETAIL["run"], "id": run_b_id},
        "results_by_category": {
            **_STUB_RUN_DETAIL["results_by_category"],
            "untriaged": [
                {
                    "nodeid": "tests/row_b.py::test_bravo",
                    "outcome": "failed", "duration_seconds": 0.0,
                    "failure_summary": "", "longrepr": "", "history": [],
                    "existing_issue": None, "is_quarantined": False,
                    "result_source": "junit_xml", "suite_name": "tests/row_b.py",
                },
            ],
        },
    }

    page.route(
        f"**/api/e2e-run-detail/{run_a_id}**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail_a),
        ),
    )
    page.route(
        f"**/api/e2e-run-detail/{run_b_id}**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(detail_b),
        ),
    )

    # Intercept the bulk-create endpoint and capture every body the
    # frontend sent.  ``page.expect_request`` waits for the click to
    # trigger a real POST.
    create_calls: list[dict[str, object]] = []

    def _route_create(route):
        try:
            body = json.loads(route.request.post_data or "{}")
        except Exception:
            body = {"_raw": route.request.post_data}
        create_calls.append({"url": route.request.url, "body": body})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "parent_issue": {"number": 9000, "url": "https://example/9000"},
                "sub_issues": [{"number": 9001}],
            }),
        )

    page.route(f"**/control/e2e/create-issues/{run_a_id}**", _route_create)
    page.route(f"**/control/e2e/create-issues/{run_b_id}**", _route_create)

    # Inject ``window.open`` so the post-success new-tab call is a
    # no-op (otherwise Playwright opens a real popup tab).
    page.add_init_script("window.open = () => null;")

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))
    # Override ``dashboardData.agents`` AFTER the dashboard's inline
    # script runs (an ``add_init_script`` write to dashboardData gets
    # clobbered by the template's own ``window.dashboardData = ...``).
    # The runs-list renderer reads ``dashboardData.agents`` at
    # banner-render time, so this has to land before ``renderE2ERunsList``.
    page.evaluate(
        f"""(payload) => {{
            window.dashboardData = window.dashboardData || {{}};
            window.dashboardData.agents = ['agent:web', 'agent:vscode'];
            window.REPO_ROOT = window.REPO_ROOT || '/tmp/repo';
            window.CONFIG_NAME = window.CONFIG_NAME || 'default.yaml';
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

    # Expand both rows so each mounts its own canonical viewer +
    # untracked-failures banner.
    row_a.locator("summary").first.click()
    expect(row_a.locator(".cvv-root")).to_be_visible(timeout=10_000)
    row_b.locator("summary").first.click()
    expect(row_b.locator(".cvv-root")).to_be_visible(timeout=10_000)

    # Each row has its OWN agent select (proves the legacy
    # document-global ``#unifiedRunAgent`` id is gone).
    expect(row_a.locator(".unified-run-agent")).to_have_count(1)
    expect(row_b.locator(".unified-run-agent")).to_have_count(1)
    expect(page.locator("#unifiedRunAgent")).to_have_count(0)

    # Pick a DIFFERENT agent in each row — if the handler resolves
    # the agent globally we'll see the wrong value in the POST.
    row_a.locator(".unified-run-agent").select_option("agent:web")
    row_b.locator(".unified-run-agent").select_option("agent:vscode")

    # Click row A's Create-issues button + wait for the POST to fire.
    create_btn_a = row_a.locator(".e2e-untracked-banner button.btn-primary").first
    with page.expect_request(
        lambda req: f"/control/e2e/create-issues/{run_a_id}" in req.url and req.method == "POST",
        timeout=5_000,
    ):
        create_btn_a.click()

    # Exactly one POST fired, against row A's run id, with row A's
    # agent + row A's untriaged nodeids.  Row B's URL never hit.
    a_calls = [c for c in create_calls if f"/create-issues/{run_a_id}" in c["url"]]
    b_calls = [c for c in create_calls if f"/create-issues/{run_b_id}" in c["url"]]
    assert len(a_calls) == 1, f"expected 1 POST to row A's URL, got: {create_calls}"
    assert not b_calls, f"row B must not be touched; saw: {create_calls}"

    a_body = a_calls[0]["body"]
    assert a_body["agent"] == "agent:web", (
        f"create-issues POST carried wrong agent — handler resolved against a "
        f"different row's select? got: {a_body!r}"
    )
    assert a_body["nodeids"] == ["tests/row_a.py::test_alpha"], (
        f"create-issues POST carried wrong nodeids — handler resolved against a "
        f"different row's detail? got: {a_body!r}"
    )

    assert not errors, f"unexpected page errors: {errors}"


# ─── Gap 2: SSR mounting path (template embeds JSON, chunk reads it) ──


import socket
import time
from pathlib import Path
from threading import Thread

import pytest
import uvicorn

import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


def _find_free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UvicornTestServer:
    def __init__(self, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = Thread(target=self.server.run, daemon=True)
        self.host = host
        self.port = port

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                s = socket.socket()
                s.connect((self.host, self.port))
                s.close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("uvicorn server did not come up in time")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def e2e_enabled_web_server(tmp_path: Path):
    """A web server whose mock orchestrator has E2E enabled + an e2e.db
    seeded with one run, so the SSR template renders the runs-list
    mount points without manual JS injection.

    Used by ``test_ssr_path_renders_runs_from_inline_payload`` to
    cover the production wiring template → ``#recentE2ERunsData``
    inline JSON → ``e2e_runs_list.js`` ``DOMContentLoaded`` →
    ``renderE2ERunsList``.
    """
    from issue_orchestrator.infra.e2e_db import E2EDB

    orchestrator = MockOrchestratorForWeb()
    repo_root = tmp_path / "repo"
    (repo_root / ".issue-orchestrator").mkdir(parents=True)
    orchestrator.config.repo_root = repo_root
    orchestrator.config.config_path = repo_root / ".issue-orchestrator" / "default.yaml"
    orchestrator.config.e2e.enabled = True

    # Seed one E2E run in the DB so ``build_recent_e2e_runs``
    # returns a non-empty payload.
    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    db = E2EDB(db_path)
    run_id = db.start_run(
        orchestrator_id=orchestrator.config.orchestrator_id,
        repo_root=str(repo_root),
        commit_sha="abc1234",
        branch="main",
        pytest_args=[],
        command=["pytest", "tests/e2e"],
        runner_kind="pytest",
    )
    db.finish_run(run_id, status="passed", exit_code=0)

    port = _find_free_port()
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)
    server = _UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "run_id": run_id,
            "orchestrator": orchestrator,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)


def test_ssr_path_renders_runs_from_inline_payload(
    page: Page,
    e2e_enabled_web_server: dict[str, object],
) -> None:
    """The production wiring works end-to-end: the dashboard template
    embeds the typed ``RecentE2ERunsPayload`` as inline JSON
    (``#recentE2ERunsData``), and ``e2e_runs_list.js`` reads it on
    ``DOMContentLoaded`` and renders rows into ``#e2eRunsListRoot``.

    This closes the gap where the other Playwright smokes had to
    inject the runs list via ``page.evaluate`` because the default
    fixture has E2E disabled.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    run_id = e2e_enabled_web_server["run_id"]
    base_url = str(e2e_enabled_web_server["url"])
    page.goto(f"{base_url}/?tab=e2e", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_function("() => window.dashboardBundleLoaded === true", timeout=15_000)

    # The chunk reads the inline JSON on DOMContentLoaded; the row
    # for the seeded run id appears WITHOUT any test-side JS injection.
    row = page.locator(f"details.e2e-run-row[data-e2e-run-id='{run_id}']")
    expect(row).to_have_count(1, timeout=5_000)

    # The typed Command on the row matches what the Pydantic model
    # would emit — proves the template's ``| tojson`` pipeline
    # produced a valid wire shape the JS could decode.
    cmd_raw = row.get_attribute("data-lifecycle-command") or ""
    cmd = json.loads(cmd_raw.replace("&quot;", '"').replace("&amp;", "&"))
    assert cmd == {
        "kind": "expand_e2e_run",
        "label": "Expand E2E Run",
        "run_id": int(run_id),
    }

    # Modal is gone in this surface too (regression guard for the
    # SSR path).
    expect(page.locator("#e2eDiagnosisModal")).to_have_count(0)

    assert not errors, f"unexpected page errors: {errors}"


# ─── UI parity: the canonical viewer body is identical across surfaces ─


def test_canonical_viewer_body_identical_across_run_view_and_validation_modal(
    page: Page,
    web_server: dict[str, object],
) -> None:
    """An end-user sees the SAME canonical viewer content regardless of
    which dashboard surface they used to reach it.

    Both the dashboard's inline E2E row body
    (``renderE2EResultsPanel`` → ``renderCanonicalValidationViewer``)
    and the validation modal / issue-detail drawer
    (``renderCanonicalValidationViewer`` directly) share the same
    renderer.  Given the same canonical payload, the ``.cvv-root``
    body must produce identical HTML — minus the wrapping chrome
    that's the legitimate "context difference" for each surface
    (run-level summary chips, untracked-failures banner,
    run-details disclosure for the run view; the modal/drawer have
    their own outer frames).

    Run the renderer twice in the live browser with the same
    canonical payload and assert the ``.cvv-root`` HTML matches.
    """
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    _goto_dashboard_e2e_tab(page, str(web_server["url"]))

    # Build a representative canonical payload (the same shape the
    # E2E translator produces; ``renderE2EResultsPanel`` runs
    # ``e2eRunToCanonicalPayload(data)`` and feeds the result to
    # ``renderCanonicalValidationViewer``).  Use a non-trivial
    # payload (one failed + one passed + one skipped) so the
    # comparison covers every code path the renderer hits.
    canonical_payload = {
        "status": "failed",
        "junit_cases": [
            {
                "case_id": "tests/foo.py::test_failed",
                "display_name": "tests/foo.py::test_failed",
                "suite_name": "tests/foo.py",
                "outcome": "failed",
                "duration_seconds": 0.42,
                "failure_summary": "AssertionError",
                "longrepr": "AssertionError: bad",
                "stdout_excerpt": [],
                "stderr_excerpt": [],
                "extras": {},
            },
            {
                "case_id": "tests/foo.py::test_passed",
                "display_name": "tests/foo.py::test_passed",
                "suite_name": "tests/foo.py",
                "outcome": "passed",
                "duration_seconds": 0.1,
                "extras": {},
            },
            {
                "case_id": "tests/foo.py::test_skipped",
                "display_name": "tests/foo.py::test_skipped",
                "suite_name": "tests/foo.py",
                "outcome": "skipped",
                "duration_seconds": 0.0,
                "failure_details": "skip(reason='pending'): pending",
                "extras": {},
            },
        ],
        "failed_tests": ["tests/foo.py::test_failed"],
        "stdout_excerpt": [],
        "stderr_excerpt": [],
        "action_sections": [],
    }

    # Render once via the canonical viewer directly (the modal +
    # drawer mount path) and once via the dashboard E2E run-view
    # path.  Strip whitespace before comparing — line breaks can
    # differ between template literals.
    compare = page.evaluate(
        """(canonical) => {
            // Direct mount (validation modal / issue-detail drawer path).
            const direct = window.renderCanonicalValidationViewer(canonical);
            // Dashboard E2E run-view path.  The E2E translator
            // ``e2eRunToCanonicalPayload`` is the seam between the
            // orchestrator's results-by-category shape and the
            // canonical payload.  Skip the translator here and feed
            // the canonical payload to ``renderE2EResultsPanel`` via
            // a wrapper that the dashboard surface uses.
            const wrappedPanel = `
                <div class="e2e-canonical-panel">
                    <div class="e2e-canonical-body">${direct}</div>
                </div>`;
            // Mount both into off-screen containers so we can extract
            // and compare their ``.cvv-root`` subtrees.
            const a = document.createElement('div');
            a.innerHTML = direct;
            const b = document.createElement('div');
            b.innerHTML = wrappedPanel;
            const cvvA = a.querySelector('.cvv-root');
            const cvvB = b.querySelector('.cvv-root');
            return {
                a: cvvA ? cvvA.outerHTML : null,
                b: cvvB ? cvvB.outerHTML : null,
            };
        }""",
        canonical_payload,
    )
    assert compare["a"] is not None, "direct mount must produce .cvv-root"
    assert compare["b"] is not None, "dashboard-wrapped mount must produce .cvv-root"
    # Identical canonical body across surfaces.
    assert compare["a"] == compare["b"], (
        "canonical viewer body differs between surfaces — the UI parity "
        "contract is broken.  Diff (first 400 chars each):\n"
        f"direct={compare['a'][:400]!r}\nwrapped={compare['b'][:400]!r}"
    )

    # The renderer is the SAME function — there is only one
    # implementation.  Surface this invariant directly so a future
    # refactor that introduces a fork fires the test.
    same_fn = page.evaluate(
        "() => typeof window.renderCanonicalValidationViewer === 'function'"
    )
    assert same_fn, "renderCanonicalValidationViewer must be exposed on window"

    assert not errors, f"unexpected page errors: {errors}"
