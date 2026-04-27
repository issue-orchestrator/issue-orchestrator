"""Dialog and timeline web route tests split from test_web."""

# ruff: noqa: F403,F405

from tests.unit import test_web as _support
from tests.unit.test_web import *  # noqa: F403

globals().update(
    {name: value for name, value in vars(_support).items() if not name.startswith("__")}
)

class TestDialogEndpoints:
    """Tests for dialog view-model endpoints."""

    def test_doctor_dialog_runs_without_orchestrator(self):
        """GET /api/dialog/doctor remains available during startup failures."""
        from issue_orchestrator.infra.doctor.types import Check, DoctorResult

        set_orchestrator(None)

        with patch(
            "issue_orchestrator.entrypoints.web_diagnostics_routes.run_doctor",
            return_value=DoctorResult([Check(name="Config", status="ok", detail="loaded")]),
        ) as mock_run_doctor:
            client = TestClient(app)
            response = client.get("/api/dialog/doctor")

        assert response.status_code == 200
        payload = response.json()
        assert payload["title"] == "Doctor"
        assert any(
            check["name"] == "Orchestrator" and check["status"] == "error"
            for check in payload["checks"]
        )
        mock_run_doctor.assert_called_once()


class TestRefreshEndpoint:
    """Test the POST /api/refresh endpoint."""

    def test_refresh_without_body(self):
        """Test refresh without body calls request_refresh."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/refresh")

            assert response.status_code == 200
            assert response.json()["status"] == "refresh_requested"
            assert "refresh" in response.json()
            mock_orch.request_refresh.assert_called_once()
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_updates_cache(self):
        """Refreshing one issue updates local cache and freshness state."""
        mock_orch = create_mock_orchestrator()
        issue = create_issue(77, "Refresh me")
        mock_orch.repository_host.get_issue.return_value = issue
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 200
            payload = response.json()
            assert payload["status"] == "refreshed"
            assert payload["issue_number"] == 77
            assert payload["is_stale"] is False
            assert payload["last_refreshed_label"] == "just now"
            assert 77 in mock_orch.state.issue_last_refreshed_at
            assert any(i.number == 77 for i in mock_orch.state.cached_queue_issues)
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_does_not_override_queue_refresh_state(self):
        """Single-issue refresh should not mutate queue refresh lifecycle state."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.queue_refresh_in_progress = False
        issue = create_issue(77, "Refresh me")
        mock_orch.repository_host.get_issue.return_value = issue
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 200
            assert mock_orch.state.queue_refresh_in_progress is False
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_not_found(self):
        """Refreshing a missing issue returns 404."""
        mock_orch = create_mock_orchestrator()
        mock_orch.repository_host.get_issue.return_value = None
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/999/refresh")
            assert response.status_code == 404
            assert "not found" in response.json()["error"].lower()
        finally:
            set_orchestrator(None)

    def test_single_issue_refresh_reports_repository_error(self):
        """Refreshing one issue reports upstream GitHub failures."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        mock_orch = create_mock_orchestrator()
        mock_orch.repository_host.get_issue.side_effect = GitHubHttpError(
            "GitHub unavailable",
            status_code=503,
            response_text='{"message":"degraded"}',
        )
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 502
            payload = response.json()
            assert payload["error"] == "Failed to refresh issue #77 from GitHub"
            assert payload["upstream_status_code"] == 503
            assert "degraded" in payload["detail"]
        finally:
            set_orchestrator(None)


class TestApiTimelineEndpoint:
    """Test the GET /api/timeline/{issue_number} endpoint."""

    def test_timeline_returns_events(self, tmp_path: Path):
        """Timeline endpoint returns stream events with artifacts."""
        from issue_orchestrator.entrypoints import web
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-returns-events"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    run_id="20260206-000000Z",
                    run_dir=str(run.run_dir),
                    artifacts=[TimelineArtifact("worktree", "Worktree", "/tmp/worktree")],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.completed",
                    issue_number=123,
                    phase="completed",
                    step="completed",
                    status="completed",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    run_dir=str(run.run_dir),
                    artifacts=[
                        TimelineArtifact("pull_request", "PR", "https://example/pr/1"),
                        TimelineArtifact("review_comment", "Review Comment", "https://example/pr/1#issuecomment-1"),
                        TimelineArtifact("completion_record", "Completion", "/tmp/worktree/completion.json"),
                    ],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert payload["issue_number"] == 123
            assert len(payload["events"]) == 2
            assert payload["events"][0]["event"] == "session.started"
            assert payload["events"][0]["artifacts"][0]["type"] == "worktree"
            assert payload["events"][0]["run_id"] == "20260206-000000Z"
            assert payload["events"][0]["run_dir"].endswith("__issue-123")
            action_types = {a["type"] for a in payload["events"][0]["actions"]}
            assert "open_path" in action_types
            assert "open_agent_log" in action_types
            assert "open_session_diagnostics" in action_types
            start_actions = payload["events"][0]["actions"]
            assert sum(1 for action in start_actions if action["type"] == "open_session_diagnostics") == 1
            assert start_actions[-1]["type"] == "open_session_diagnostics"
            completion_artifacts = {a["type"] for a in payload["events"][1]["artifacts"]}
            assert "pull_request" in completion_artifacts
            assert "review_comment" in completion_artifacts
            assert "completion_record" in completion_artifacts
            completion_actions = payload["events"][1]["actions"]
            assert any(
                action["type"] == "open_url" and "issuecomment" in action.get("url", "")
                for action in completion_actions
            )
            completion_labels = [action["label"] for action in completion_actions]
            review_index = completion_labels.index("Open Review Comment ↗")
            diagnostics_index = completion_labels.index("Diagnostics…")
            assert review_index < diagnostics_index
        finally:
            set_orchestrator(None)

    def test_timeline_cycles_include_orchestrator_phase_events_within_active_cycle(self):
        """Validation/queue orchestration events should remain in the same active cycle."""
        mock_orch = create_mock_orchestrator()
        stream = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    phase="in_progress",
                    status="started",
                ),
                build_timeline_event(
                    "validation.completed",
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    phase="orchestrator",
                    status="completed",
                ),
                build_timeline_event(
                    "review.queued",
                    event_id="e3",
                    timestamp="2026-02-06T00:02:00Z",
                    phase="orchestrator",
                    status="started",
                ),
                build_timeline_event(
                    "review.started",
                    event_id="e4",
                    timestamp="2026-02-06T00:03:00Z",
                    phase="reviewing",
                    status="started",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["cycles"]) == 1
            cycle = _first_cycle(payload)
            assert cycle["phases"] == ["in_progress", "orchestrator", "reviewing"]
            assert [event["event"] for event in cycle["events"]] == [
                "session.started",
                "validation.completed",
                "review.queued",
                "review.started",
            ]
        finally:
            set_orchestrator(None)

    def test_issue_detail_returns_payload(self):
        """Issue detail endpoint returns drawer payload."""
        payload = fetch_issue_detail_payload(
            [build_timeline_event("session.started", summary="started")]
        )
        assert payload["issue_number"] == 123
        assert payload["title"] == "Detail Issue"
        assert "summary" in payload
        assert "events" in payload
        assert "cycles" in payload
        assert "actions" in payload

    def test_issue_detail_includes_retry_publish_action_when_available(self):
        payload = fetch_issue_detail_payload(
            [build_timeline_event("session.started", summary="started")],
            can_retry_publish=True,
        )
        assert any(action.get("id") == "retry_publish" for action in payload["actions"])

    def test_issue_detail_survives_failed_coder_with_stale_review_start(self):
        """A failed coder terminal state owns the cycle even if review start leaked in."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="coding-start",
                timestamp="2026-02-09T10:00:00Z",
                agent="agent:backend",
                run_dir="/tmp/run-330",
            ),
            build_timeline_event(
                "review.started",
                event_id="review-start",
                timestamp="2026-02-09T10:10:00Z",
                reviewer_agent="agent:reviewer",
                run_dir="/tmp/review-330",
            ),
            build_timeline_event(
                "session.failed",
                event_id="coding-failed",
                timestamp="2026-02-09T10:20:00Z",
                status="failed",
                agent="agent:backend",
                run_dir="/tmp/run-330",
                summary="Exceeded timeout",
            ),
        ])

        cycle = payload["lifecycle"]["current"]["issue_lifecycles"][0]["cycles"][0]
        assert cycle["coder"]["kind"] == "failed_coding_attempt"
        assert cycle["review"] == {
            "kind": "review_not_reached",
            "reason": "coding_failed",
        }
        assert cycle["outcome"] == "failed"

    def test_issue_detail_starts_new_lifecycle_after_completion_without_review(self):
        """Signal path: a new coding session after completion becomes a new lifecycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T11:00:00Z",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T11:30:00Z",
                status="completed",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_review_continuation_stays_in_same_lifecycle(self):
        """Signal path: completion followed by review remains one lifecycle/run."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.started",
                event_id="e3",
                timestamp="2026-02-09T10:31:00Z",
                status="started",
                phase="reviewing",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.changes_requested",
                event_id="e4",
                timestamp="2026-02-09T10:32:00Z",
                status="failed",
                phase="reviewing",
                rework_cycle=0,
                reviewer_agent="agent:reviewer",
            ),
            build_timeline_event(
                "rework.started",
                event_id="e5",
                timestamp="2026-02-09T10:40:00Z",
                status="started",
                phase="rework",
                rework_cycle=1,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e6",
                timestamp="2026-02-09T11:00:00Z",
                status="completed",
                rework_cycle=1,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        assert [cycle["iteration"] for cycle in journey_cycles] == [1, 2]
        assert {cycle["lifecycle"] for cycle in journey_cycles} == {1}
        assert payload["run_count"] == 1

    def test_issue_detail_manual_unblock_without_event_starts_new_lifecycle(self):
        """Manual label removal (no issue.unblocked event) still creates a new run lifecycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-08T10:00:00Z",
                status="started",
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-08T10:30:00Z",
                status="completed",
            ),
            build_timeline_event(
                "issue.blocked",
                event_id="e3",
                timestamp="2026-02-08T10:40:00Z",
                status="failed",
                phase="blocked",
            ),
            build_timeline_event(
                "session.started",
                event_id="e4",
                timestamp="2026-02-09T09:00:00Z",
                status="started",
                logical_run=2,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e5",
                timestamp="2026-02-09T09:30:00Z",
                status="completed",
                logical_run=2,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_signal_events_split_from_legacy_lifecycle(self):
        """Legacy timeline followed by signal-era events should split runs."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-08T10:00:00Z",
                status="started",
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-08T10:30:00Z",
                status="completed",
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        lifecycles = [cycle["lifecycle"] for cycle in journey_cycles]
        assert lifecycles[1] > lifecycles[0]
        assert payload["run_count"] == 2

    def test_issue_detail_includes_cycle_run_id_for_latest_run_filtering(self):
        """Journey cycles should carry run_id + cycle_in_run for latest-run rendering."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                run_id="run-1",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                run_id="run-1",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.started",
                event_id="e3",
                timestamp="2026-02-09T11:00:00Z",
                status="started",
                run_id="run-2",
                logical_run=2,
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e4",
                timestamp="2026-02-09T11:30:00Z",
                status="completed",
                run_id="run-2",
                logical_run=2,
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 2
        assert [cycle["run_id"] for cycle in journey_cycles] == ["run-1", "run-2"]
        assert [cycle["cycle_in_run"] for cycle in journey_cycles] == [1, 1]

    def test_issue_detail_drops_claim_preamble_when_real_cycles_exist(self):
        """Claim-only preamble should not appear as its own numbered cycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "claim.acquired",
                event_id="e1",
                timestamp="2026-02-09T09:50:00Z",
                status="completed",
                phase="in_progress",
            ),
            build_timeline_event(
                "session.started",
                event_id="e2",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e3",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 1
        step_events = [step["event"] for step in journey_cycles[0]["steps"]]
        assert "claim.acquired" not in step_events

    def test_issue_detail_drops_claim_event_inside_signal_cycle(self):
        """Claim events are hidden even when they share the active signal cycle."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-09T10:00:00Z",
                status="started",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "claim.acquired",
                event_id="e2",
                timestamp="2026-02-09T10:01:00Z",
                status="completed",
                rework_cycle=0,
            ),
            build_timeline_event(
                "session.completed",
                event_id="e3",
                timestamp="2026-02-09T10:30:00Z",
                status="completed",
                rework_cycle=0,
            ),
        ])

        runs = payload["runs"]
        journey_cycles = [cycle for run in runs for cycle in run["cycles"]]
        assert len(journey_cycles) == 1
        step_events = [step["event"] for step in journey_cycles[0]["steps"]]
        assert "claim.acquired" not in step_events

    def test_issue_detail_reports_expected_history_missing_when_empty(self):
        """Issue detail should surface diagnostic when history exists but timeline is empty."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload["summary"].get("timeline_diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "expected_history_missing"
            assert "session_history_present" in diagnostic["signals"]
            assert diagnostic["expected_timeline_store"].endswith("/timeline.sqlite")
            assert diagnostic["expected_timeline_store_exists"] is False
            assert "Timeline data missing" in payload["status_explanation"]
        finally:
            set_orchestrator(None)

    def test_issue_detail_does_not_report_missing_timeline_when_events_present(self):
        """Diagnostic should be absent when timeline events exist for the issue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    event_id="e1",
                    timestamp="2026-02-09T10:00:00Z",
                    status="started",
                    phase="in_progress",
                    rework_cycle=0,
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            summary = payload.get("summary", {})
            assert summary.get("timeline_diagnostic") is None
            assert "Timeline data missing" not in payload.get("status_explanation", "")
        finally:
            set_orchestrator(None)

    def test_issue_detail_surfaces_current_run_validation_failure(self, tmp_path: Path):
        """Issue detail should expose current run validation failures even before a timeline failure event exists."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-diagnostic"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=123)
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_status": "failed",
                "validation_reason": ".venv/bin/python missing",
                "validation_record_path": ".issue-orchestrator/sessions/r1/validation-record.json",
                "validation_stdout": ".issue-orchestrator/sessions/r1/validation-stdout.log",
                "validation_stderr": ".issue-orchestrator/sessions/r1/validation-stderr.log",
            },
        )
        (run.run_dir / "validation-record.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "suite": "publish_gate",
                    "head_sha": "deadbeef",
                    "passed": False,
                    "exit_code": 2,
                    "command": "make validate",
                    "started_at": "2026-03-22T04:53:14Z",
                    "ended_at": "2026-03-22T04:53:58Z",
                    "timed_out": False,
                }
            ),
            encoding="utf-8",
        )
        (run.run_dir / "validation-stdout.log").write_text(
            "FAILED tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open\n",
            encoding="utf-8",
        )
        (run.run_dir / "validation-stderr.log").write_text("make: *** [validate] Error 2\n", encoding="utf-8")
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    event_id="e1",
                    timestamp="2026-02-09T10:00:00Z",
                    status="started",
                    phase="in_progress",
                    run_dir=str(run.run_dir),
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload["summary"].get("run_diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "validation_failed"
            assert diagnostic["reason"] == ".venv/bin/python missing"
            assert diagnostic["failed_tests_preview"] == [
                "tests/unit/test_web.py::TestProviderCircuitsEndpoint::test_get_provider_circuits_open"
            ]
            assert any(action.get("id") == "open_validation_failure" for action in payload["actions"])
            assert "Current run failed validation" in payload["status_explanation"]
        finally:
            set_orchestrator(None)

    def test_issue_detail_prefers_validation_failure_over_timeline_missing(self, tmp_path: Path):
        """Current-run validation failure should remain the primary explanation when both diagnostics apply."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput

        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-run-diagnostic-priority"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "coding-1", issue_number=123)
        session_output.update_manifest(
            run.run_dir,
            {
                "validation_status": "failed",
                "validation_reason": ".venv/bin/python missing",
            },
        )
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
                worktree_path=worktree,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            assert "Current run failed validation" in payload["status_explanation"]
            assert "Timeline data missing" not in payload["status_explanation"]
            assert payload["summary"]["timeline_diagnostic"]["state"] == "expected_history_missing"
            assert payload["summary"]["run_diagnostic"]["state"] == "validation_failed"
        finally:
            set_orchestrator(None)

    def test_issue_detail_survives_action_decoration_failure(self):
        """A single bad event artifact must not break issue-detail rendering."""
        mock_orch = create_mock_orchestrator()
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                build_timeline_event(
                    "session.started",
                    issue_number=123,
                    event_id="e-bad",
                    run_dir="/tmp/does-not-exist/run",
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    status="started",
                    phase="in_progress",
                ),
                build_timeline_event(
                    "issue.pr_created",
                    issue_number=123,
                    event_id="e-good",
                    status="completed",
                    phase="done",
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            assert payload["events"][0]["event"] == "session.started"
            action_types = {
                action.get("type")
                for action in (payload["events"][0].get("actions") or [])
                if isinstance(action, dict)
            }
            assert "show_actions_error" in action_types
            assert "actions_error" in payload["events"][0]
            assert payload["events"][1]["event"] == "issue.pr_created"
        finally:
            set_orchestrator(None)

    def test_timeline_reports_expected_history_missing_when_empty(self):
        """Timeline endpoint should include diagnostics for missing expected history."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.pending_reviews = [
            PendingReview(
                issue_key=FakeIssueKey(name="123"),
                _issue_number=123,
                pr_number=456,
                pr_url="https://example.com/pr/456",
                branch_name="123-test",
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload.get("diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "expected_history_missing"
            assert "pending_review_present" in diagnostic["signals"]
            assert diagnostic["expected_timeline_store"].endswith("/timeline.sqlite")
            assert diagnostic["expected_timeline_store_exists"] is False
        finally:
            set_orchestrator(None)

    def test_issue_detail_reports_logical_semantics_missing_when_events_lack_fields(self):
        """Issue detail should fail fast on events missing logical semantics."""
        mock_orch = create_mock_orchestrator()
        mock_orch.state.session_history = [
            SessionHistoryEntry(
                issue_number=123,
                title="Issue 123",
                agent_type="agent:web",
                status="completed",
                runtime_minutes=5,
            ),
        ]
        mock_orch.deps.timeline_reader.read.return_value = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-09T10:00:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                ),
            ],
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/issue-detail/123")
            assert response.status_code == 200
            payload = response.json()
            diagnostic = payload["summary"].get("timeline_diagnostic")
            assert diagnostic is not None
            assert diagnostic["state"] == "logical_semantics_missing"
            assert diagnostic["dropped_missing_semantics"] == 1
            assert "logical_semantics_missing" in diagnostic["signals"]
        finally:
            set_orchestrator(None)

    def test_issue_detail_latest_logical_run_keeps_review_with_rework(self):
        """Latest run must be logical lifecycle, not physical run_id ordering."""
        payload = fetch_issue_detail_payload([
            build_timeline_event(
                "session.started",
                event_id="e1",
                timestamp="2026-02-16T02:13:47Z",
                status="started",
                run_id="20260216-071346Z",
                rework_cycle=0,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e2",
                timestamp="2026-02-16T02:19:00Z",
                status="completed",
                run_id="20260216-071346Z",
                rework_cycle=0,
            ),
            build_timeline_event(
                "review.started",
                event_id="e3",
                timestamp="2026-02-16T02:19:10Z",
                status="started",
                run_id="20260216-075116Z",
                rework_cycle=0,
                task="review",
            ),
            build_timeline_event(
                "review.changes_requested",
                event_id="e4",
                timestamp="2026-02-16T02:22:00Z",
                status="failed",
                run_id="20260216-075116Z",
                rework_cycle=0,
                task="review",
            ),
            build_timeline_event(
                "rework.started",
                event_id="e5",
                timestamp="2026-02-16T02:47:51Z",
                status="started",
                run_id="20260216-074751Z",
                rework_cycle=1,
                agent="agent:backend",
            ),
            build_timeline_event(
                "session.completed",
                event_id="e6",
                timestamp="2026-02-16T03:00:00Z",
                status="completed",
                run_id="20260216-074751Z",
                rework_cycle=1,
            ),
        ])

        assert payload["run_count"] == 1
        latest_run = _latest_run(payload)
        review_events = [
            step["event"]
            for cycle in latest_run["cycles"]
            for step in cycle.get("steps", [])
            if str(step.get("event", "")).startswith("review.")
        ]
        assert review_events, "Latest logical run should include review events"
        assert latest_run.get("session_run_ids") == [
            "20260216-071346Z",
            "20260216-075116Z",
            "20260216-074751Z",
        ]

    def test_timeline_filters_label_churn_events(self, tmp_path: Path):
        """Timeline endpoint omits low-signal issue.labels_changed churn events."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-filter-churn"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="issue.labels_changed",
                    issue_number=123,
                    phase="in_progress",
                    step="labels_changed",
                    status="completed",
                    level="detail",
                    summary="label update",
                    parent_key="issue:123",
                    artifacts=[],
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.completed",
                    issue_number=123,
                    phase="completed",
                    step="completed",
                    status="completed",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=1,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert len(payload["events"]) == 1
            assert payload["events"][0]["event"] == "session.completed"
            assert any(
                action["type"] == "open_session_diagnostics"
                for action in payload["events"][0]["actions"]
            )
        finally:
            set_orchestrator(None)

    def test_timeline_keeps_pr_pending_removal_label_event(self, tmp_path: Path):
        """Timeline should retain pr-pending removal because it changes run boundaries."""
        from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
        mock_orch = create_mock_orchestrator()
        session_output = FileSystemSessionOutput()
        worktree = tmp_path / "wt-timeline-pr-pending"
        worktree.mkdir(parents=True)
        run = session_output.start_run(worktree, "issue-123", issue_number=123)
        (run.run_dir / "ui-session.log").write_text("agent output\n", encoding="utf-8")
        claude_log = run.run_dir / "claude.jsonl"
        claude_log.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
        session_output.update_manifest(run.run_dir, {"claude_log_path": str(claude_log)})

        stream = TimelineStream(
            issue_number=123,
            events=[
                TimelineEvent(
                    event_id="e1",
                    timestamp="2026-02-06T00:00:00Z",
                    event="issue.labels_changed",
                    issue_number=123,
                    phase="in_progress",
                    step="labels_changed",
                    status="completed",
                    level="detail",
                    summary="removed pr-pending",
                    parent_key="issue:123",
                    artifacts=[],
                    removed=["pr-pending"],
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="orchestrator",
                    logical_run=2,
                    logical_cycle=1,
                    logical_phase="orchestrator",
                ),
                TimelineEvent(
                    event_id="e2",
                    timestamp="2026-02-06T00:01:00Z",
                    event="session.started",
                    issue_number=123,
                    phase="in_progress",
                    step="started",
                    status="started",
                    level="phase",
                    summary=None,
                    parent_key="session:issue-123",
                    artifacts=[],
                    run_dir=str(run.run_dir),
                    timeline_schema_version=TIMELINE_SCHEMA_VERSION,
                    event_intent="coding",
                    logical_run=2,
                    logical_cycle=1,
                    logical_phase="coding",
                ),
            ],
        )
        mock_orch.deps.timeline_reader.read.return_value = stream

        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.get("/api/timeline/123")
            assert response.status_code == 200
            payload = response.json()
            assert [event["event"] for event in payload["events"]] == [
                "issue.labels_changed",
                "session.started",
            ]
        finally:
            set_orchestrator(None)

    def test_refresh_with_inflight_stable_ids(self):
        """Test refresh with inflight_stable_ids parameter."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                json={"inflight_stable_ids": ["issue-1", "issue-2"]}
            )

            assert response.status_code == 200
            mock_orch.request_refresh.assert_called_once()
            call_args = mock_orch.request_refresh.call_args
            assert call_args.kwargs["inflight_stable_ids"] == {"issue-1", "issue-2"}
        finally:
            set_orchestrator(None)

    def test_refresh_with_empty_inflight_ids(self):
        """Test refresh with empty inflight_stable_ids list."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                json={"inflight_stable_ids": []}
            )

            assert response.status_code == 200
            call_args = mock_orch.request_refresh.call_args
            assert call_args.kwargs["inflight_stable_ids"] == set()
        finally:
            set_orchestrator(None)

    def test_refresh_ignores_malformed_json(self):
        """Test refresh ignores malformed JSON."""
        from issue_orchestrator.entrypoints import web
        mock_orch = create_mock_orchestrator()
        mock_orch.request_refresh = MagicMock()
        set_orchestrator(mock_orch)

        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh",
                content="not valid json",
                headers={"Content-Type": "application/json"}
            )

            assert response.status_code == 200
            mock_orch.request_refresh.assert_called_once()
        finally:
            set_orchestrator(None)

    def test_refresh_when_orchestrator_not_running(self):
        """Test refresh returns 503 when orchestrator not initialized."""
        from issue_orchestrator.entrypoints import web
        set_orchestrator(None)

        client = TestClient(app)
        response = client.post("/api/refresh")

        assert response.status_code == 503
        assert "error" in response.json()

    def test_refresh_visibility_updates_state(self):
        """Test visibility refresh endpoint stores current visible issues."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/refresh/visibility", json={"issues": [12, "13", -1, "bad"]})
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert mock_orch.state.ui_visible_issue_numbers == [12, 13]
            assert mock_orch.state.ui_visible_updated_at > 0
        finally:
            set_orchestrator(None)

    def test_refresh_visibility_requires_valid_json(self):
        """Test visibility refresh endpoint rejects invalid payload."""
        mock_orch = create_mock_orchestrator()
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post(
                "/api/refresh/visibility",
                content="not-json",
                headers={"Content-Type": "application/json"},
            )
            assert response.status_code == 400
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_updates_cached_queue(self):
        """Test single issue refresh updates existing cached issue and timestamp."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = create_issue(7, "new title")
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "refreshed"
            assert response.json()["in_scope"] is True
            assert response.json()["updated"] is True
            assert mock_orch.state.cached_queue_issues[0].title == "new title"
            assert mock_orch.state.issue_refresh_timestamps[7] > 0
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_does_not_inject_out_of_scope_issue(self):
        """Out-of-scope single refresh should not inject issue into queue."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.state.issue_refresh_timestamps = {7: 100.0, 999: 200.0}
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = create_issue(
            7, "other scope", labels=["agent:other"]
        )
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "rejected_out_of_scope"
            assert response.json()["in_scope"] is False
            assert not any(issue.number == 7 for issue in mock_orch.state.cached_queue_issues)
            assert 7 not in mock_orch.state.issue_refresh_timestamps
            assert 999 not in mock_orch.state.issue_refresh_timestamps
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_404_when_missing(self):
        """Test single issue refresh returns 404 when issue cannot be fetched."""
        mock_orch = create_mock_orchestrator()
        mock_orch.repository_host = MagicMock()
        mock_orch.repository_host.get_issue.return_value = None
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/77/refresh")
            assert response.status_code == 404
        finally:
            set_orchestrator(None)

    def test_refresh_single_issue_rejects_closed_issue(self):
        """Closed issues should never be re-admitted to cached queue via refresh."""
        mock_orch = create_mock_orchestrator()
        mock_orch.config.filtering.label = "agent:web"
        mock_orch.state.cached_queue_issues = [create_issue(7, "old title")]
        mock_orch.state.issue_refresh_timestamps = {7: 100.0}
        mock_orch.repository_host = MagicMock()
        closed_issue = create_issue(7, "closed issue", labels=["agent:web"])
        closed_issue.state = "closed"
        mock_orch.repository_host.get_issue.return_value = closed_issue
        set_orchestrator(mock_orch)
        try:
            client = TestClient(app)
            response = client.post("/api/issues/7/refresh")
            assert response.status_code == 200
            assert response.json()["status"] == "rejected_out_of_scope"
            assert response.json()["in_scope"] is False
            assert not any(issue.number == 7 for issue in mock_orch.state.cached_queue_issues)
            assert 7 not in mock_orch.state.issue_refresh_timestamps
        finally:
            set_orchestrator(None)
