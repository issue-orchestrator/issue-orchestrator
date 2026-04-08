"""Browser-driven test for the E2E run drawer's issue affordances.

This is the highest-fidelity check we have that the dashboard's E2E
timeline actually renders clickable issue links: it stages a captured
real-data fixture into a tmp_path, points a real uvicorn server at it,
loads the dashboard in Playwright, opens the run drawer, switches to
the Timeline tab, and asserts that the rendered HTML contains issue
link anchors that resolve to the dashboard issue detail drawer.

It catches a class of bug the JSON-only integration test cannot:
- frontend regressions in ``renderTimeline`` that drop ``issue_numbers``
- CSS that hides ``.timeline-issue-links``
- broken ``openIssueDetail`` plumbing
- contract drift between ``/control/e2e/run/{id}/timeline`` and the JS
  that consumes its response

The fixture is the same one used by
``tests/integration/test_e2e_timeline_real_fixture.py``, so a
single ``snapshot_e2e_run.py`` capture covers both layers.
"""

from __future__ import annotations

import shutil
import socket
import time
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
import issue_orchestrator.entrypoints.web as web_module
from issue_orchestrator.entrypoints.web import app
from tests.fixtures.web_contract_mocks import MockOrchestratorForWeb


FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "e2e_runs"
RUN_87_FIXTURE = FIXTURE_DIR / "run_87"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


_MINIMAL_CONFIG_YAML = """
worktrees:
  base: "../"

agents:
  "agent:test":
    prompt: "test.md"
"""


def _stage_fixture(fixture: Path, tmp_path: Path) -> Path:
    """Mirror the integration-test fixture stager so the live endpoint
    code path runs unmodified against on-disk data.

    Also writes a minimal default.yaml so the run-details endpoint
    (which the dashboard fetches in parallel with the timeline endpoint
    when opening the run drawer) can resolve a config without crashing.
    The control endpoints we actually care about — the timeline + the
    issue-detail drawer — do not depend on this config.
    """
    repo_root = tmp_path / "repo"
    state_dir = repo_root / ".issue-orchestrator" / "state"
    config_dir = repo_root / ".issue-orchestrator" / "config"
    state_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    wt_state = tmp_path / "repo-e2e-worktree" / ".issue-orchestrator" / "state"
    wt_state.mkdir(parents=True)

    shutil.copy(fixture / "e2e.db", repo_root / ".issue-orchestrator" / "e2e.db")
    shutil.copy(fixture / "base_timeline.sqlite", state_dir / "timeline.sqlite")
    shutil.copy(fixture / "worktree_timeline.sqlite", wt_state / "timeline.sqlite")
    (config_dir / "default.yaml").write_text(_MINIMAL_CONFIG_YAML)
    return repo_root


class _UvicornTestServer:
    def __init__(self, host: str, port: int) -> None:
        self.config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread: Thread | None = None

    def start(self) -> None:
        self.thread = Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.1)
                if sock.connect_ex((self.config.host, int(self.config.port))) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(
            f"Test server failed to start on {self.config.host}:{self.config.port}"
        )

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture
def fixture_web_server(tmp_path: Path) -> dict[str, object]:
    """Stage run_87 into tmp_path and start a real uvicorn server pointing at it.

    The control endpoints (``/control/e2e/run/{id}/timeline`` etc.) read
    timeline data straight from disk based on the ``repo_root`` query
    param, so the orchestrator only needs to expose ``config.repo_root``
    pointing at the staged layout. ``deps.timeline_store`` is wired to a
    real SqliteTimelineStore for completeness.
    """
    if not RUN_87_FIXTURE.exists():
        pytest.skip(
            f"run_87 fixture missing at {RUN_87_FIXTURE}. "
            "Capture with: scripts/snapshot_e2e_run.py --run-id 87"
        )

    repo_root = _stage_fixture(RUN_87_FIXTURE, tmp_path)
    base_store = SqliteTimelineStore(
        db_path=repo_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
    )

    orchestrator = MockOrchestratorForWeb()
    orchestrator.config.repo_root = repo_root
    # The dashboard view model uses config.config_path.name as the JS
    # CONFIG_NAME global; the run-details endpoint then passes it to
    # _load_config_by_name. Point at the staged config so the lookup
    # resolves to a real file.
    orchestrator.config.config_path = (
        repo_root / ".issue-orchestrator" / "config" / "default.yaml"
    )

    # Build a realistic deps object: real timeline_store + reader for
    # the endpoints that read them, and MagicMock for the optional
    # accessories the issue-detail action-applier code touches
    # (publish_recovery, etc.). Using MagicMock as the base lets any
    # incidental attribute access return a sensible default instead
    # of crashing with AttributeError.
    from unittest.mock import MagicMock
    from issue_orchestrator.execution.timeline_reader import DefaultTimelineReader

    deps = MagicMock()
    deps.timeline_store = base_store
    deps.timeline_reader = DefaultTimelineReader(base_store)
    # publish_recovery.can_retry_publish is consulted by the issue-detail
    # action applier — return False so the "retry publish" affordance
    # stays hidden (it's orthogonal to this test's contract).
    deps.publish_recovery.can_retry_publish.return_value = False
    orchestrator.deps = deps

    port = _find_free_port()
    original = web_module.get_orchestrator()
    web_module.set_orchestrator(orchestrator)

    server = _UvicornTestServer("127.0.0.1", port)
    server.start()
    try:
        yield {
            "url": f"http://127.0.0.1:{port}",
            "repo_root": repo_root,
            "run_id": 87,
        }
    finally:
        server.stop()
        web_module.set_orchestrator(original)


_TEST_4057_NODEID = (
    "tests/e2e/test_issue_4057_production_flow.py"
    "::test_4057_production_real_agents_publish_gate_and_diagnostics"
)
_TEST_INFLIGHT_NODEID = (
    "tests/e2e/test_inflight_refresh_discovers_issue.py"
    "::test_inflight_refresh_discovers_issue"
)


def test_run_drawer_timeline_renders_clickable_issue_links(
    page: Page, fixture_web_server: dict[str, object]
) -> None:
    """Validate row-level affordance placement and click-through content.

    Per the PR #5709 review, this test must prove:

    1. The Timeline tab actually rendered real event data from the
       endpoint payload — not just an empty skeleton. We look up the
       canonical ``test_4057`` event by its summary text and assert it
       exists before probing its issue-link content.
    2. The ``test_4057`` row carries exactly ``#5705`` and ``#5706`` —
       scoped to that specific ``.timeline-event`` card, not to any
       global anchor anywhere in the modal. This catches misattached
       links that "exist somewhere" but on the wrong row.
    3. A neighboring row (``test_inflight_refresh_discovers_issue``)
       carries ``#5705`` but NOT ``#5706`` — a negative assertion that
       prevents false positives from cross-row contamination.
    4. Clicking ``#5705`` causes the issue-detail drawer to render
       journey content (``.journey-run``) — not just an optimistic
       title that shows before the fetch completes. We also assert
       the status line is no longer "Loading..." or "unavailable",
       and no ``.timeline-empty`` placeholder is present.

    Action-button assertions are intentionally out of scope: the staged
    fixture uses sanitized run_dir paths that don't exist on disk, so
    session-action decoration emits warnings that are orthogonal to the
    row/affordance contract under test.
    """
    base_url = fixture_web_server["url"]
    run_id = fixture_web_server["run_id"]

    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    # Open the run drawer directly via the JS entry point. The dashboard
    # tab navigation is exercised by the existing dashboard_flow tests;
    # this test focuses on the run drawer + timeline tab.
    page.evaluate(f"showUnifiedRunView({run_id})")

    modal = page.locator("#e2eDiagnosisModal.visible")
    expect(modal).to_be_visible(timeout=5000)

    # Switch to the Timeline tab. The button is rendered only when the
    # endpoint returned a non-empty timeline.
    timeline_tab_btn = page.locator(
        "#e2eDiagnosisModal .e2e-run-tab[data-tab='timeline']"
    )
    expect(timeline_tab_btn).to_be_visible(timeout=5000)
    timeline_tab_btn.click()

    timeline_panel = page.locator("#e2eRunTimelineTab")
    expect(timeline_panel).to_be_visible(timeout=5000)

    # --- Ask 4: prove the Timeline tab actually rendered real event data ---
    # Find the specific test_4057 event card by its summary text. If the
    # endpoint returned nothing, this fails fast — no amount of global
    # anchor matching would save us.
    test_4057_summary = timeline_panel.locator(
        ".timeline-event",
        has=page.locator(".timeline-summary", has_text=_TEST_4057_NODEID),
    )
    # Two summary rows (started + completed) share the nodeid in the
    # rendered timeline, so we scope assertions to the first occurrence.
    test_4057_event = test_4057_summary.first
    expect(test_4057_event).to_be_visible(timeout=5000)
    expect(test_4057_event.locator(".timeline-summary").first).to_contain_text(
        _TEST_4057_NODEID,
    )

    # --- Ask 1: scope link assertions to the test_4057 row ---
    test_4057_links = test_4057_event.locator(".timeline-issue-links a")
    expect(test_4057_links).to_have_count(2, timeout=5000)
    link_texts = sorted(test_4057_links.all_text_contents())
    assert link_texts == ["#5705", "#5706"], (
        f"test_4057 row should carry exactly [#5705, #5706] but got "
        f"{link_texts!r}"
    )

    # --- Ask 2: negative assertion on a neighboring row ---
    # test_inflight_refresh_discovers_issue carries ONLY #5705 per the
    # fixture's expected.json. Catch misattached links (e.g. #5706
    # leaking from test_4057) by asserting the link set.
    test_inflight_event = timeline_panel.locator(
        ".timeline-event",
        has=page.locator(".timeline-summary", has_text=_TEST_INFLIGHT_NODEID),
    ).first
    expect(test_inflight_event).to_be_visible(timeout=5000)
    inflight_links = test_inflight_event.locator(".timeline-issue-links a")
    expect(inflight_links).to_have_count(1, timeout=5000)
    inflight_texts = sorted(inflight_links.all_text_contents())
    assert inflight_texts == ["#5705"], (
        f"test_inflight_refresh row should carry exactly [#5705] but got "
        f"{inflight_texts!r} — a link from another row has leaked in"
    )

    # --- Ask 3: click-through must prove the drawer loaded data ---
    # Click the #5705 link scoped to the test_4057 row (not the
    # matching one in the inflight row) so the click-through assertion
    # is about the test_4057 affordance specifically.
    test_4057_links.filter(has_text="#5705").first.click()

    detail_drawer = page.locator("#issueDetailDrawer.visible")
    expect(detail_drawer).to_be_visible(timeout=5000)
    # Title alone is not sufficient — it's set before the fetch completes.
    expect(page.locator("#issueDetailTitle")).to_contain_text("Issue #5705")

    # Wait for the journey to actually render. The endpoint returns
    # events structured as runs/cycles, so on success #issueDetailJourney
    # contains at least one .journey-run. No .timeline-empty placeholder
    # should be present.
    journey = page.locator("#issueDetailJourney")
    expect(journey.locator(".journey-run").first).to_be_visible(timeout=5000)
    expect(journey.locator(".timeline-empty")).to_have_count(0)

    # And the status line must no longer show the loading / unavailable text.
    status_el = page.locator("#issueDetailStatus")
    status_text = status_el.text_content() or ""
    assert "Loading issue detail" not in status_text, (
        f"drawer still showing loading state: {status_text!r}"
    )
    assert "Issue detail unavailable" not in status_text, (
        f"drawer showing unavailable state: {status_text!r}"
    )
    assert "Failed to load" not in status_text, (
        f"drawer showing failure state: {status_text!r}"
    )

    assert errors == [], f"Unexpected page errors: {errors}"
