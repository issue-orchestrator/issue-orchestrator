"""Unit tests for BoardSnapshotBuilder.

The builder is observation-flavored: it gathers facts from a fabricated
OrchestratorState plus injected callables (timeline reader, log tail
provider, clock) and makes no decisions. These tests verify the fact
projection, the injected-clock age computation, the timeline issue
selection, and the defensive bounds documented on the builder.
"""

from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.control.board_snapshot_builder import (
    MAX_LINE_CHARS,
    MAX_LIST_ENTRIES,
    MAX_TIMELINE_ISSUES,
    BoardSnapshotBuilder,
    StateBoardSnapshotProvider,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.triage_session import TriageSessionFlavor
from issue_orchestrator.control.session_routing import PendingSessionQueues
from issue_orchestrator.domain.models import (
    AgentConfig,
    DependencyProblem,
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.ports.timeline_store import TimelineRecord
from tests.unit.session_run_helpers import make_session_run_assets

FIXED_NOW = datetime(2026, 7, 10, 12, 0, 0)


class FakeTimelineReader:
    """Records calls and serves canned TimelineRecord lists per issue."""

    def __init__(self, records_by_issue: dict[int, list[TimelineRecord]] | None = None):
        self.records_by_issue = records_by_issue or {}
        self.calls: list[tuple[int, int]] = []

    def __call__(self, issue_number: int, limit: int) -> list[TimelineRecord]:
        self.calls.append((issue_number, limit))
        return self.records_by_issue.get(issue_number, [])


class FakeLogTail:
    """Records the requested line count and serves canned lines."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.calls: list[int] = []

    def __call__(self, n: int) -> list[str]:
        self.calls.append(n)
        return self.lines


def _record(event_id: str) -> TimelineRecord:
    return TimelineRecord(
        event_id=event_id,
        timestamp="2026-07-10T11:00:00+00:00",
        event="session.started",
        data={"agent": "agent:test"},
    )


def _make_builder(
    *,
    timeline_reader: FakeTimelineReader | None = None,
    log_tail: FakeLogTail | None = None,
) -> BoardSnapshotBuilder:
    return BoardSnapshotBuilder(
        timeline_reader=timeline_reader or FakeTimelineReader(),
        log_tail_provider=log_tail or FakeLogTail([]),
        clock=lambda: FIXED_NOW,
    )


def _make_session(
    run_assets: SessionRunAssets,
    prompt_path: Path,
    *,
    issue_number: int,
    title: str = "Test issue",
    started_at: datetime,
    task: TaskKind = TaskKind.CODE,
) -> Session:
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=task),
        issue=Issue(number=issue_number, title=title, labels=["agent:test"]),
        agent_config=AgentConfig(prompt_path=prompt_path, model="sonnet"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=run_assets.worktree_path,
        branch_name=f"{issue_number}-test",
        run_assets=run_assets,
        started_at=started_at,
    )


@pytest.fixture
def run_assets(tmp_path: Path) -> SessionRunAssets:
    return make_session_run_assets(tmp_path / "worktree")


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.md"
    path.write_text("prompt", encoding="utf-8")
    return path


class TestSessions:
    def test_session_ages_computed_from_injected_clock(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        s1 = _make_session(
            run_assets,
            prompt_path,
            issue_number=101,
            title="Older session",
            started_at=datetime(2026, 7, 10, 11, 15, 0),
        )
        s2 = _make_session(
            run_assets,
            prompt_path,
            issue_number=102,
            title="Newer session",
            started_at=datetime(2026, 7, 10, 11, 58, 0),
            task=TaskKind.REVIEW,
        )
        state = OrchestratorState(active_sessions=[s1, s2])

        snapshot = _make_builder().build(state)

        assert len(snapshot.sessions) == 2
        first, second = snapshot.sessions
        assert first.issue_number == 101
        assert first.issue_title == "Older session"
        assert first.agent_type == "agent:test"
        assert first.session_type == "code"
        assert first.status == "running"
        assert first.started_at == "2026-07-10T11:15:00"
        assert first.age_minutes == 45
        assert first.terminal_id == "issue-101"
        assert second.age_minutes == 2
        assert second.session_type == "review"

    def test_sessions_capped_at_max_entries(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        sessions = [
            _make_session(
                run_assets,
                prompt_path,
                issue_number=1000 + i,
                started_at=datetime(2026, 7, 10, 11, 0, 0),
            )
            for i in range(MAX_LIST_ENTRIES + 1)
        ]
        state = OrchestratorState(active_sessions=sessions)

        snapshot = _make_builder().build(state)

        assert len(state.active_sessions) == 101
        assert len(snapshot.sessions) == MAX_LIST_ENTRIES
        assert snapshot.sessions[0].issue_number == 1000  # first entries win


class TestQueuesBlockedAndFailures:
    def _state_with_queues(self) -> OrchestratorState:
        return OrchestratorState(
            paused=True,
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey("201"),
                    pr_number=7,
                    pr_url="https://github.com/o/r/pull/7",
                    branch_name="201-branch",
                    _issue_number=201,
                ),
            ],
            pending_retrospective_reviews=[
                PendingRetrospectiveReview(
                    issue_key=FakeIssueKey("207"),
                    issue_number=207,
                    issue_title="Existing implementation",
                    agent_label="agent:test",
                    trigger_label="io:review-existing",
                    prior_pr_number=12,
                ),
            ],
            pending_reworks=[
                PendingRework(
                    issue_key=FakeIssueKey("202"),
                    agent_type="agent:test",
                    rework_cycle=2,
                    issue_number=202,
                    pr_number=8,
                    feedback="Missing tests",
                ),
            ],
            pending_triage_reviews=[
                PendingTriageReview(issue_number=203, title="Triage batch review", flavor=TriageSessionFlavor.BATCH_REVIEW),
            ],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=204,
                    issue_title="Validation victim",
                    agent_label="agent:test",
                    worktree_path="/wt/204",
                    branch_name="204-branch",
                    original_prompt=None,
                    validation_error="pytest exploded",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                ),
            ],
            priority_queue=[42],
            dependency_problems={
                205: DependencyProblem(
                    issue_number=205,
                    issue_title="Blocked feature",
                    blocked_by=[(99, "Dependency issue", "open")],
                    summary="Blocked by 1 open dependency",
                ),
            },
        )

    @staticmethod
    def _planner_queue_names() -> set[str]:
        """Every planner-consumed pending queue, derived from the contract.

        ``OrchestratorSnapshot`` is the planner's input; its ``pending_*``
        fields plus ``priority_queue`` are exactly the capacity-consuming
        queues. Deriving the set here (instead of hand-listing) makes this
        test fail when a future queue is added to the planner contract but
        not projected onto the board snapshot.
        """
        import dataclasses

        from issue_orchestrator.control.planner_types import OrchestratorSnapshot

        return {
            f.name
            for f in dataclasses.fields(OrchestratorSnapshot)
            if f.name.startswith("pending_") or f.name == "priority_queue"
        }

    def test_queue_entries_cover_every_queue(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        by_queue = {entry.queue: entry for entry in snapshot.queues}
        expected = self._planner_queue_names()
        assert "pending_retrospective_reviews" in expected  # sanity: derivation works
        assert set(by_queue) == expected
        assert by_queue["pending_reviews"].issue_number == 201
        assert "PR #7" in by_queue["pending_reviews"].detail
        assert by_queue["pending_retrospective_reviews"].issue_number == 207
        assert "io:review-existing" in by_queue["pending_retrospective_reviews"].detail
        assert "prior PR #12" in by_queue["pending_retrospective_reviews"].detail
        assert by_queue["pending_reworks"].issue_number == 202
        assert "rework cycle 2" in by_queue["pending_reworks"].detail
        assert "Missing tests" in by_queue["pending_reworks"].detail
        assert by_queue["pending_triage"].issue_number == 203
        assert by_queue["pending_triage"].detail == "Triage batch review"
        assert by_queue["pending_validation_retries"].issue_number == 204
        assert "pytest exploded" in by_queue["pending_validation_retries"].detail
        assert by_queue["priority_queue"].issue_number == 42
        assert by_queue["priority_queue"].detail == ""

    def test_retrospective_detail_without_prior_pr(self) -> None:
        state = OrchestratorState(
            pending_retrospective_reviews=[
                PendingRetrospectiveReview(
                    issue_key=FakeIssueKey("208"),
                    issue_number=208,
                    issue_title="No prior PR",
                    agent_label="agent:test",
                    trigger_label="io:review-existing",
                ),
            ],
        )

        snapshot = _make_builder().build(state)

        (entry,) = snapshot.queues
        assert entry.queue == "pending_retrospective_reviews"
        assert "io:review-existing" in entry.detail
        assert "prior PR" not in entry.detail

    def test_paused_and_generated_at_reflect_state_and_clock(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        assert snapshot.orchestrator_paused is True
        assert snapshot.generated_at == FIXED_NOW.isoformat()

    def test_blocked_issues_mirror_dependency_problems(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        assert len(snapshot.blocked_issues) == 1
        blocked = snapshot.blocked_issues[0]
        assert blocked.issue_number == 205
        assert blocked.issue_title == "Blocked feature"
        assert blocked.summary == "Blocked by 1 open dependency"
        assert blocked.blocked_by == [(99, "Dependency issue", "open")]

    def test_failures_taken_from_explicit_parameter(self) -> None:
        failures = [
            DiscoveredFailure(
                issue_number=206,
                issue_title="Failed thing",
                failure_reason="timed_out",
                artifact_hints=(
                    "/runs/issue-206/failure-diagnostic.md",
                    "/runs/issue-206/analysis.json",
                ),
            ),
        ]

        snapshot = _make_builder().build(OrchestratorState(), failures=failures)

        assert len(snapshot.recent_failures) == 1
        failure = snapshot.recent_failures[0]
        assert failure.issue_number == 206
        assert failure.issue_title == "Failed thing"
        assert failure.failure_reason == "timed_out"
        # #6762: hints gathered at the discovery seam are projected verbatim
        # — never invented, never discarded.
        assert failure.artifact_hints == [
            "/runs/issue-206/failure-diagnostic.md",
            "/runs/issue-206/analysis.json",
        ]

    def test_failure_without_hints_projects_empty_hints(self) -> None:
        """A failure source with no artifacts yields empty hints, not fabricated ones."""
        failures = [
            DiscoveredFailure(
                issue_number=207, issue_title="No artifacts", failure_reason="failed"
            ),
        ]

        snapshot = _make_builder().build(OrchestratorState(), failures=failures)

        assert snapshot.recent_failures[0].artifact_hints == []

    def test_queues_capped_at_max_entries(self) -> None:
        state = OrchestratorState(priority_queue=list(range(MAX_LIST_ENTRIES + 50)))

        snapshot = _make_builder().build(state)

        assert len(snapshot.queues) == MAX_LIST_ENTRIES

    def test_long_queue_detail_is_truncated(self) -> None:
        state = OrchestratorState(
            pending_triage_reviews=[
                PendingTriageReview(issue_number=203, title="x" * (MAX_LINE_CHARS + 100), flavor=TriageSessionFlavor.BATCH_REVIEW),
            ],
        )

        snapshot = _make_builder().build(state)

        assert len(snapshot.queues[0].detail) == MAX_LINE_CHARS

    def test_unresolvable_rework_issue_number_fails_fast(self) -> None:
        state = OrchestratorState(
            pending_reworks=[
                PendingRework(issue_key=FakeIssueKey("not-a-number"), agent_type="agent:test"),
            ],
        )

        with pytest.raises(ValueError, match="not-a-number"):
            _make_builder().build(state)


class TestTimeline:
    def test_focus_issue_timeline_included_first_and_deduped(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        session = _make_session(
            run_assets,
            prompt_path,
            issue_number=101,
            started_at=datetime(2026, 7, 10, 11, 0, 0),
        )
        state = OrchestratorState(active_sessions=[session])
        failures = [
            DiscoveredFailure(issue_number=206, issue_title="Failed", failure_reason="failed"),
            # Duplicate of the focus issue - must not produce a second extract.
            DiscoveredFailure(issue_number=300, issue_title="Also focus", failure_reason="failed"),
        ]
        reader = FakeTimelineReader({300: [_record("evt-focus")], 101: [_record("evt-s")]})
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(state, focus_issue=300, failures=failures, timeline_limit=5)

        assert [t.issue_number for t in snapshot.timeline] == [300, 101, 206]
        assert reader.calls == [(300, 5), (101, 5), (206, 5)]
        focus_extract = snapshot.timeline[0]
        assert focus_extract.records == [
            {
                "event_id": "evt-focus",
                "timestamp": "2026-07-10T11:00:00+00:00",
                "event": "session.started",
                "data": {"agent": "agent:test"},
            },
        ]
        assert snapshot.timeline[2].records == []  # no records for issue 206

    def test_timeline_issues_capped(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        sessions = [
            _make_session(
                run_assets,
                prompt_path,
                issue_number=1000 + i,
                started_at=datetime(2026, 7, 10, 11, 0, 0),
            )
            for i in range(MAX_TIMELINE_ISSUES + 2)
        ]
        reader = FakeTimelineReader()
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(
            OrchestratorState(active_sessions=sessions), focus_issue=999
        )

        assert len(snapshot.timeline) == MAX_TIMELINE_ISSUES
        assert snapshot.timeline[0].issue_number == 999  # focus issue always first
        assert len(reader.calls) == MAX_TIMELINE_ISSUES

    def test_timeline_records_capped_at_limit(self) -> None:
        reader = FakeTimelineReader({500: [_record(f"evt-{i}") for i in range(10)]})
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(OrchestratorState(), focus_issue=500, timeline_limit=3)

        assert len(snapshot.timeline[0].records) == 3


class TestLogTail:
    def test_log_tail_provider_receives_requested_line_count(self) -> None:
        log_tail = FakeLogTail(["line one", "line two"])
        builder = _make_builder(log_tail=log_tail)

        snapshot = builder.build(OrchestratorState(), log_tail_lines=50)

        assert log_tail.calls == [50]
        assert snapshot.log_tail == ["line one", "line two"]

    def test_log_lines_truncated_and_capped(self) -> None:
        long_line = "y" * (MAX_LINE_CHARS + 200)
        log_tail = FakeLogTail([long_line, "short", "extra-beyond-cap"])
        builder = _make_builder(log_tail=log_tail)

        snapshot = builder.build(OrchestratorState(), log_tail_lines=2)

        # Provider over-delivered: list capped at log_tail_lines, lines clipped.
        assert len(snapshot.log_tail) == 2
        assert snapshot.log_tail[0] == "y" * MAX_LINE_CHARS
        assert snapshot.log_tail[1] == "short"


class TestStateBoardSnapshotProvider:
    """StateBoardSnapshotProvider binds the builder to live orchestrator state.

    It is the composition-root seam behind the BoardSnapshotProvider port:
    the launcher calls ``snapshot(focus_issue)`` and the provider must read
    the *current* state through the getter and forward this tick's
    ``discovered_failures`` (a per-tick buffer the orchestrator clears after
    planning).
    """

    def test_snapshot_reads_current_state_and_forwards_failures(self) -> None:
        state = OrchestratorState(paused=True)
        state.discovered_failures.append(
            DiscoveredFailure(
                issue_number=301, issue_title="Boom", failure_reason="failed"
            )
        )
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert snapshot.orchestrator_paused is True
        assert [f.issue_number for f in snapshot.recent_failures] == [301]

    def test_focus_issue_is_forwarded_to_the_builder(self) -> None:
        timeline_reader = FakeTimelineReader({777: [_record("e1")]})
        provider = StateBoardSnapshotProvider(
            _make_builder(timeline_reader=timeline_reader),
            lambda: OrchestratorState(),
        )

        snapshot = provider.snapshot(777)

        assert [t.issue_number for t in snapshot.timeline] == [777]
        assert timeline_reader.calls and timeline_reader.calls[0][0] == 777

    def test_queued_failure_context_survives_cleared_tick_buffer(self) -> None:
        """P1 regression: discovery on tick N, launch on tick N+1.

        ``discovered_failures`` is cleared after planning, so by the time the
        queued failure investigation launches the buffer is empty. The typed
        failure context preserved on the queue item (via the real queue
        owner, PendingSessionQueues) must appear in ``recent_failures`` — a
        failure investigation whose snapshot is missing its own triggering
        failure was the defect.
        """
        state = OrchestratorState()
        failure = DiscoveredFailure(
            issue_number=904,
            issue_title="Broken thing",
            failure_reason="timed_out",
            artifact_hints=("/runs/issue-904/failure-diagnostic.md",),
        )
        # Tick N: producer queues the investigation with its failure context.
        PendingSessionQueues(state).queue_failure_investigation(
            904, "Investigate: Broken thing (timed_out)", failure=failure
        )
        # Tick boundary: the orchestrator clears the per-tick fact buffer.
        state.discovered_failures.clear()
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(904)

        assert [
            (f.issue_number, f.issue_title, f.failure_reason, f.artifact_hints)
            for f in snapshot.recent_failures
        ] == [
            (
                904,
                "Broken thing",
                "timed_out",
                ["/runs/issue-904/failure-diagnostic.md"],
            )
        ]
        # The triggering failure also drives a timeline extract for the issue.
        assert [t.issue_number for t in snapshot.timeline] == [904]

    def test_live_buffer_wins_over_queued_context_on_duplicate_issue(self) -> None:
        """Merge rule: live buffer first, queued context deduped by issue."""
        state = OrchestratorState()
        PendingSessionQueues(state).queue_failure_investigation(
            904,
            "Investigate: Broken thing (timed_out)",
            failure=DiscoveredFailure(
                issue_number=904, issue_title="Stale title", failure_reason="timed_out"
            ),
        )
        PendingSessionQueues(state).queue_failure_investigation(
            905,
            "Investigate: Other thing (failed)",
            failure=DiscoveredFailure(
                issue_number=905, issue_title="Other thing", failure_reason="failed"
            ),
        )
        # This tick re-discovered issue 904's failure with fresher facts.
        state.discovered_failures.append(
            DiscoveredFailure(
                issue_number=904, issue_title="Fresh title", failure_reason="failed"
            )
        )
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert [
            (f.issue_number, f.issue_title) for f in snapshot.recent_failures
        ] == [(904, "Fresh title"), (905, "Other thing")]

    def test_batch_review_queue_items_add_no_failures(self) -> None:
        """Batch reviews carry no failure context and must not fabricate any."""
        state = OrchestratorState()
        PendingSessionQueues(state).queue_batch_review(7, "Triage Batch")
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert snapshot.recent_failures == []
