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

import json
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
RUN_FIXTURE = FIXTURE_DIR / "run_88"
RUN_FIXTURE_ID = 88

# Issue used for the session-log click-through in run_88. This issue
# appears in both test_inflight_refresh_discovers_issue and test_4057
# rows (per expected.json), so whichever row the test clicks, the
# synthetic session recording is reachable. Its compact branch label
# renders as "inflight-discovery".
TEST_CLICK_ISSUE_NUMBER = 5723
TEST_CLICK_ISSUE_LABEL = "inflight-discovery"
# Second issue on the test_4057 row — used to verify multi-affordance
# row rendering with distinct labels.
TEST_CLICK_ISSUE_NUMBER_2 = 5724
TEST_CLICK_ISSUE_LABEL_2 = "ui-surface-provider-cir\u2026"


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

# Recognizable text written into the synthetic terminal recording so the
# Playwright test can prove the session-log endpoint returned real content.
_SYNTHETIC_SESSION_OUTPUT = "PLAYWRIGHT-FIXTURE-SESSION-STARTED"
_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP = "PLAYWRIGHT-FIXTURE-AGENT-READY"


def _materialize_synthetic_session_dir(session_dir: Path) -> None:
    """Create a real run_dir with a non-empty terminal-recording.jsonl.

    The session-log action decorator (``_preferred_run_scoped_session_action``
    in web.py) refuses to emit an ``open_agent_log`` affordance unless the
    event's ``run_dir`` actually exists on disk. The session-replay endpoint
    additionally requires ``terminal-recording.jsonl`` to be non-empty. This
    helper synthesizes the minimum layout that both checks accept.

    The file contains:
      - one ``resize`` event (provides initial_geometry)
      - two ``output`` events whose base64 text is chosen to be searchable
        from the Playwright test via ``sessionReplayState.events``.
    """
    import base64

    session_dir.mkdir(parents=True, exist_ok=True)
    recording = session_dir / "terminal-recording.jsonl"
    events = [
        {
            "event_type": "resize",
            "offset_ms": 0,
            "rows": 24,
            "cols": 80,
        },
        {
            "event_type": "output",
            "offset_ms": 100,
            "data_b64": base64.b64encode(
                (_SYNTHETIC_SESSION_OUTPUT + "\r\n").encode("utf-8"),
            ).decode("ascii"),
        },
        {
            "event_type": "output",
            "offset_ms": 250,
            "data_b64": base64.b64encode(
                (_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP + "\r\n").encode("utf-8"),
            ).decode("ascii"),
        },
    ]
    recording.write_text(
        "\n".join(json.dumps(e, sort_keys=True) for e in events) + "\n",
    )


def _wire_event_to_session_dir(
    worktree_db: Path,
    issue_number: int,
    event_name: str,
    session_dir: Path,
) -> None:
    """Point one worktree-timeline event at a real, on-disk run_dir.

    Updates both the ``run_dir`` column and the ``data_json.run_dir`` field
    of a single matching row so the action decorator's path-exists check
    succeeds and a ``Session Recording`` button appears in the issue-detail
    drawer for that event.

    Raises if the target event is not found — silent misses would leave
    the Playwright test asserting against an empty state.
    """
    import sqlite3

    run_dir_str = str(session_dir)
    with sqlite3.connect(str(worktree_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT sequence, data_json FROM timeline_events
            WHERE issue_number = ? AND event = ? AND run_dir != ''
            ORDER BY sequence ASC LIMIT 1
            """,
            (issue_number, event_name),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"Cannot wire session dir: no {event_name} event with a "
                f"non-empty run_dir found for issue {issue_number}"
            )
        data = json.loads(row["data_json"]) if row["data_json"] else {}
        if isinstance(data, dict):
            data["run_dir"] = run_dir_str
            for key in (
                "worktree_path",
                "session_prompt_path",
                "completion_path_absolute",
            ):
                if key in data and isinstance(data[key], str):
                    if key == "worktree_path":
                        data[key] = str(session_dir.parent)
                    else:
                        data[key] = run_dir_str + "/" + Path(data[key]).name
        conn.execute(
            "UPDATE timeline_events SET run_dir = ?, data_json = ? WHERE sequence = ?",
            (run_dir_str, json.dumps(data), row["sequence"]),
        )
        conn.commit()


def _stage_fixture(fixture: Path, tmp_path: Path) -> Path:
    """Mirror the integration-test fixture stager so the live endpoint
    code path runs unmodified against on-disk data.

    In addition to the base fixture copy, this stager materializes ONE
    real session run_dir at ``tmp_path/session1`` containing a
    synthetic ``terminal-recording.jsonl``, and rewires the
    ``TEST_CLICK_ISSUE_NUMBER`` event to reference it. This lets the
    Playwright test verify the full click-through from timeline row →
    issue detail drawer → session recording modal → terminal content.
    Without this step the fixture has no session logs to view.

    Also writes a minimal default.yaml so the run-details endpoint
    (which the dashboard fetches in parallel with the timeline
    endpoint when opening the run drawer) can resolve a config without
    crashing.
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

    # Materialize a real session run_dir and wire the target issue's
    # agent.coding_started event to point at it. The run_dir must
    # contain a non-empty terminal-recording.jsonl for both the action
    # decorator and the session-replay endpoint to surface real data.
    session_dir = tmp_path / "session1"
    _materialize_synthetic_session_dir(session_dir)
    _wire_event_to_session_dir(
        wt_state / "timeline.sqlite",
        issue_number=TEST_CLICK_ISSUE_NUMBER,
        event_name="agent.coding_started",
        session_dir=session_dir,
    )
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
    if not RUN_FIXTURE.exists():
        pytest.skip(
            f"{RUN_FIXTURE.name} fixture missing at {RUN_FIXTURE}. "
            f"Capture with: scripts/snapshot_e2e_run.py --run-id {RUN_FIXTURE_ID}"
        )

    repo_root = _stage_fixture(RUN_FIXTURE, tmp_path)
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
            "run_id": RUN_FIXTURE_ID,
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
    2. The ``test_4057`` row carries exactly the two expected issue
       affordances (for run_88: 5723 + 5724, rendered as compact
       branch labels "inflight-discovery (5723)" and
       "ui-surface-provider-cir… (5724)"), scoped to that specific
       ``.timeline-event`` card so misattached links on the wrong row
       are caught.
    3. A neighboring row (``test_inflight_refresh_discovers_issue``)
       carries ONLY issue 5723 — a negative assertion that prevents
       false positives from cross-row contamination.
    4. Clicking the ``inflight-discovery (5723)`` link causes the
       issue-detail drawer to render journey content
       (``.journey-run``) — not just an optimistic title that shows
       before the fetch completes. We also assert the status line is
       no longer "Loading..." or "unavailable", and no
       ``.timeline-empty`` placeholder is present.

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
    # Affordances now render as "label (N)" with a hover title carrying
    # the full branch name. For run_88, test_4057 carries 5723 and 5724:
    #
    #   "inflight-discovery (5723)"        ← same issue as test_inflight
    #   "ui-surface-provider-cir… (5724)"  ← truncated by 24-char cap
    #
    test_4057_links = test_4057_event.locator(".timeline-issue-links a")
    expect(test_4057_links).to_have_count(2, timeout=5000)
    link_texts = sorted(test_4057_links.all_text_contents())
    expected_texts = sorted([
        f"{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})",
        f"{TEST_CLICK_ISSUE_LABEL_2} ({TEST_CLICK_ISSUE_NUMBER_2})",
    ])
    assert link_texts == expected_texts, (
        f"test_4057 row should carry {expected_texts!r} but got {link_texts!r}"
    )
    # Hover titles must carry the full (untruncated) branch name so users
    # can reclaim the detail hidden by the 24-char cap.
    for link in test_4057_links.all():
        title = link.get_attribute("title") or ""
        assert title.startswith(f"{TEST_CLICK_ISSUE_NUMBER}-") or title.startswith(
            f"{TEST_CLICK_ISSUE_NUMBER_2}-"
        ), (
            f"test_4057 affordance is missing its full branch_name title: "
            f"got {title!r}"
        )

    # --- Ask 2: negative assertion on a neighboring row ---
    # test_inflight_refresh_discovers_issue carries ONLY issue 5723 per
    # the fixture's expected.json. Catch misattached links (e.g. 5724
    # leaking from test_4057) by asserting the link set.
    test_inflight_event = timeline_panel.locator(
        ".timeline-event",
        has=page.locator(".timeline-summary", has_text=_TEST_INFLIGHT_NODEID),
    ).first
    expect(test_inflight_event).to_be_visible(timeout=5000)
    inflight_links = test_inflight_event.locator(".timeline-issue-links a")
    expect(inflight_links).to_have_count(1, timeout=5000)
    inflight_texts = sorted(inflight_links.all_text_contents())
    assert inflight_texts == [
        f"{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})"
    ], (
        f"test_inflight_refresh row should carry only "
        f"[{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})] "
        f"but got {inflight_texts!r} — a link from another row has leaked in"
    )

    # --- Ask 3: click-through must prove the drawer loaded data ---
    # Click the inflight-discovery link scoped to the test_4057 row
    # (not the matching one in the inflight row) so the click-through
    # assertion is about the test_4057 affordance specifically.
    test_4057_links.filter(
        has_text=f"{TEST_CLICK_ISSUE_LABEL} ({TEST_CLICK_ISSUE_NUMBER})"
    ).first.click()

    detail_drawer = page.locator("#issueDetailDrawer.visible")
    expect(detail_drawer).to_be_visible(timeout=5000)
    # Title alone is not sufficient — it's set before the fetch completes.
    expect(page.locator("#issueDetailTitle")).to_contain_text(
        f"Issue #{TEST_CLICK_ISSUE_NUMBER}"
    )

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

    # --- Session Recording click-through ---
    # The fixture stager wired one agent.coding_started event for
    # issue 5705 at a real tmp_path run_dir with a synthetic
    # terminal-recording.jsonl. The action decorator therefore emits
    # a "Session Recording" button on that event; click it and verify
    # the session-replay modal opens AND loads real terminal content
    # from the endpoint.
    #
    # Proving click-through is not enough: the modal title is set
    # optimistically before the fetch completes, same as the issue
    # detail drawer. We additionally assert
    #   (a) #sessionReplayPath carries the expected run_dir
    #   (b) sessionReplayState has > 0 events
    #   (c) the decoded events contain our synthetic recognizable text
    # so a future regression in the recording endpoint, the PTY event
    # decoding, or the replay initialization cannot silently pass.
    #
    # The E2E run drawer is expected to REMAIN visible behind the
    # issue detail drawer — closing the issue drawer should return the
    # user to the run drawer, not the dashboard. The stacking contract:
    #
    #   .modal-overlay (#e2eDiagnosisModal) .........  z-index 30
    #   #issueDetailDrawer ..........................  z-index 35
    #   #modalOverlay (session-replay, elevated) ....  z-index 45
    #
    # so every interaction layer above is reachable without dismissing
    # the one beneath. We verify that invariant here.
    expect(page.locator("#e2eDiagnosisModal.visible")).to_have_count(
        1, timeout=5000,
    )

    session_recording_btn = journey.locator(
        ".timeline-action-btn", has_text="Session Recording"
    ).first
    expect(session_recording_btn).to_be_visible(timeout=5000)
    session_recording_btn.scroll_into_view_if_needed()
    session_recording_btn.click()

    modal_overlay = page.locator("#modalOverlay.visible")
    expect(modal_overlay).to_be_visible(timeout=5000)

    session_replay_path = page.locator("#sessionReplayPath")
    expect(session_replay_path).to_be_visible(timeout=5000)
    session_path_text = session_replay_path.text_content() or ""
    assert "session1" in session_path_text and "terminal-recording.jsonl" in session_path_text, (
        f"session replay path does not reference the synthetic recording: "
        f"{session_path_text!r}"
    )

    # The progress readout shows "N / M events" once replay has
    # rendered the full stream. Poll until N > 0 so we're not racing
    # the initial fetch.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        text = page.locator("#sessionReplayProgressText").text_content() or ""
        if "/" in text and not text.startswith("0 /"):
            break
        page.wait_for_timeout(100)
    progress_text = page.locator("#sessionReplayProgressText").text_content() or ""
    assert "/" in progress_text, (
        f"session replay progress text not populated: {progress_text!r}"
    )
    assert not progress_text.startswith("0 /"), (
        f"session replay loaded zero events — endpoint returned empty "
        f"content: {progress_text!r}"
    )

    # Decode the raw events from sessionReplayState and verify the
    # synthetic output text round-tripped from the fixture through
    # the endpoint and back to the browser. This is what "reasonable
    # data" means for a session log in this test.
    #
    # ``sessionReplayState`` is declared with ``let`` at script scope
    # so it is NOT a property of ``window``; accessing it bare inside
    # the eval expression resolves the script-scoped binding.
    decoded_output = page.evaluate(
        """
        (() => {
            try {
                if (typeof sessionReplayState === 'undefined' || !sessionReplayState) {
                    return null;
                }
                const events = sessionReplayState.events || [];
                const chunks = [];
                for (const e of events) {
                    if (e && e.event_type === 'output' && e.data_b64) {
                        try {
                            chunks.push(atob(e.data_b64));
                        } catch (err) {
                            chunks.push(`<decode-error: ${err.message}>`);
                        }
                    }
                }
                return { count: events.length, text: chunks.join('') };
            } catch (err) {
                return { error: String(err) };
            }
        })()
        """
    )
    assert decoded_output is not None, (
        "sessionReplayState is not initialized after clicking Session Recording"
    )
    assert decoded_output.get("count", 0) >= 2, (
        f"session replay loaded fewer events than the fixture wrote "
        f"(expected >= 2 from 1 resize + 2 output, got "
        f"{decoded_output.get('count')})"
    )
    assert _SYNTHETIC_SESSION_OUTPUT in decoded_output.get("text", ""), (
        f"synthetic session text {_SYNTHETIC_SESSION_OUTPUT!r} not found in "
        f"decoded terminal output: {decoded_output.get('text')!r}"
    )
    assert _SYNTHETIC_SESSION_OUTPUT_FOLLOWUP in decoded_output.get("text", ""), (
        f"follow-up synthetic text {_SYNTHETIC_SESSION_OUTPUT_FOLLOWUP!r} "
        f"not found in decoded terminal output: {decoded_output.get('text')!r}"
    )

    assert errors == [], f"Unexpected page errors: {errors}"
