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

    # Make deps.timeline_store available too — required by the issue-detail
    # endpoint that openIssueDetail() ends up calling on click.
    class _Deps:
        pass

    deps = _Deps()
    deps.timeline_store = base_store
    orchestrator.deps = deps  # type: ignore[attr-defined]

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


def test_run_drawer_timeline_renders_clickable_issue_links(
    page: Page, fixture_web_server: dict[str, object]
) -> None:
    """Open the run #87 drawer and verify the Timeline tab shows real issue links.

    The expected.json from the fixture confirms which test rows should
    carry which issue affordances under the user-view-filter contract.
    We assert that the canonical "test_4057" row (the failed test that
    prompted this whole effort) renders both #5705 and #5706 as
    clickable anchors in the Timeline tab.

    Issues with only debug-tagged events (5700, 5701, 5702, 5704, 5707)
    are intentionally NOT rendered in the user view because clicking
    them would open an empty drawer; that contract is pinned by the
    integration-level click-through test, not here.
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

    # The Timeline tab panel must contain at least one issue affordance.
    issue_links_container = page.locator(
        "#e2eDiagnosisModal .timeline-issue-links"
    ).first
    expect(issue_links_container).to_be_visible(timeout=5000)

    # Both 5705 and 5706 belong to the failed test_4057 row (run_87).
    # They're attached to a real agent run, so the user-view filter
    # keeps them. We verify both render as clickable anchors.
    issue_5705_link = page.locator(
        "#e2eDiagnosisModal .timeline-issue-links a", has_text="#5705"
    ).first
    expect(issue_5705_link).to_be_visible(timeout=5000)

    issue_5706_link = page.locator(
        "#e2eDiagnosisModal .timeline-issue-links a", has_text="#5706"
    ).first
    expect(issue_5706_link).to_be_visible(timeout=5000)

    # Click through #5705 and verify the issue detail drawer actually
    # opens with content — guards against the click-through producing
    # an empty drawer (the bug the integration test also pins).
    issue_5705_link.click()
    detail_drawer = page.locator("#issueDetailDrawer.visible")
    expect(detail_drawer).to_be_visible(timeout=5000)
    expect(page.locator("#issueDetailTitle")).to_contain_text("Issue #5705")

    assert errors == [], f"Unexpected page errors: {errors}"
