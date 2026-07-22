"""Round-trip and fail-fast tests for the BoardSnapshot domain type.

The snapshot crosses a JSON file boundary between the orchestrator and a
tech_lead/tech-lead agent session, so these tests exercise both directions
(``to_dict``/``write`` for the producer, ``from_dict``/``read`` for the
consumer) and assert the reader rejects payloads it does not understand.
"""

from pathlib import Path

import pytest

from issue_orchestrator.domain.board_snapshot import (
    BOARD_SNAPSHOT_SCHEMA_VERSION,
    BoardAreaSignal,
    BoardBlockedIssue,
    BoardCaseFile,
    BoardE2EChronicFailure,
    BoardE2EHealth,
    BoardE2ERun,
    BoardFailure,
    BoardQueueEntry,
    BoardSessionInfo,
    BoardShippedFix,
    BoardSnapshot,
    BoardTimelineExtract,
)


def _sample_snapshot() -> BoardSnapshot:
    return BoardSnapshot(
        generated_at="2026-07-10T12:00:00+00:00",
        orchestrator_paused=True,
        sessions=[
            BoardSessionInfo(
                issue_number=101,
                issue_title="Fix flaky test",
                agent_type="agent:backend",
                session_type="code",
                status="running",
                started_at="2026-07-10T11:30:00+00:00",
                age_minutes=30,
                terminal_id="issue-101",
                idle_minutes=22,
                commits_ahead=0,
                last_activity_at="2026-07-10T11:38:00+00:00",
            ),
        ],
        queues=[
            BoardQueueEntry(
                queue="pending_reviews",
                issue_number=102,
                detail="PR #7 awaiting review (https://github.com/o/r/pull/7)",
            ),
            BoardQueueEntry(queue="priority_queue", issue_number=103, detail=""),
        ],
        blocked_issues=[
            BoardBlockedIssue(
                issue_number=104,
                issue_title="Blocked feature",
                summary="Blocked by 1 open dependency",
                blocked_by=[(99, "Dependency issue", "open")],
            ),
        ],
        recent_failures=[
            BoardFailure(
                issue_number=105,
                issue_title="Broken migration",
                failure_reason="timed_out",
                artifact_hints=[".issue-orchestrator/sessions/r1/terminal-recording.jsonl"],
            ),
        ],
        case_files=[
            BoardCaseFile(
                issue_number=700,
                title="Pattern case file: db-timeout",
                comment_count=3,
                updated_at="2026-07-10T11:45:00+00:00",
                area="db",
            )
        ],
        area_signals=[
            BoardAreaSignal(area="db", distinct_patterns=1, shipped_fixes=2)
        ],
        recent_shipped_fixes=[
            BoardShippedFix(
                issue_number=699,
                title="Repair DB retry seam",
                pr_url="https://github.com/o/r/pull/699",
                area="db",
                merged_at="2026-07-09T12:00:00+00:00",
            )
        ],
        timeline=[
            BoardTimelineExtract(
                issue_number=101,
                records=[
                    {
                        "event_id": "evt-1",
                        "timestamp": "2026-07-10T11:31:00+00:00",
                        "event": "session.started",
                        "data": {"agent": "agent:backend"},
                    },
                ],
            ),
        ],
        log_tail=["[PLAN] 2 action(s)", "[TICK] completed"],
        e2e_health=BoardE2EHealth(
            enabled=True,
            expected_interval_minutes=240,
            stale=True,
            nonpassing_streak=3,
            quarantine_count=2,
            last_run=BoardE2ERun(
                id=115,
                status="warning",
                started_at="2026-07-17T22:49:14+00:00",
                age_minutes=1150,
                duration_seconds=413.8,
                failed_count=1,
                passed_count=1,
            ),
            recent_runs=(
                BoardE2ERun(
                    id=115,
                    status="warning",
                    started_at="2026-07-17T22:49:14+00:00",
                    age_minutes=1150,
                    duration_seconds=413.8,
                    failed_count=1,
                    passed_count=1,
                ),
                BoardE2ERun(
                    id=114,
                    status="failed",
                    started_at="2026-07-17T17:15:28+00:00",
                    age_minutes=1484,
                    duration_seconds=None,
                    failed_count=1,
                    passed_count=9,
                ),
            ),
            chronic_failures=(
                BoardE2EChronicFailure(
                    nodeid="tests/e2e/test_x.py::test_chronic",
                    fail_count=18,
                    tracking_issue=6822,
                    tracking_resolved=False,
                ),
                BoardE2EChronicFailure(
                    nodeid="tests/e2e/test_y.py::test_untracked",
                    fail_count=3,
                    tracking_issue=None,
                    tracking_resolved=False,
                ),
            ),
        ),
    )


class TestBoardSnapshotRoundTrip:
    def test_to_dict_from_dict_round_trip_preserves_everything(self) -> None:
        original = _sample_snapshot()

        recovered = BoardSnapshot.from_dict(original.to_dict())

        assert recovered == original

    def test_blocked_by_round_trips_as_tuples(self) -> None:
        """blocked_by entries stay (number, title, state) tuples after a
        round-trip, not JSON-flavored lists."""
        recovered = BoardSnapshot.from_dict(_sample_snapshot().to_dict())

        assert recovered.blocked_issues[0].blocked_by == [(99, "Dependency issue", "open")]
        assert isinstance(recovered.blocked_issues[0].blocked_by[0], tuple)

    def test_empty_snapshot_round_trips(self) -> None:
        original = BoardSnapshot(
            generated_at="2026-07-10T12:00:00+00:00",
            orchestrator_paused=False,
        )

        recovered = BoardSnapshot.from_dict(original.to_dict())

        assert recovered == original
        assert recovered.schema_version == BOARD_SNAPSHOT_SCHEMA_VERSION

    def test_hung_evidence_fields_survive_the_round_trip(self) -> None:
        """idle_minutes/commits_ahead/last_activity_at cross the JSON boundary."""
        recovered = BoardSnapshot.from_dict(_sample_snapshot().to_dict())

        session = recovered.sessions[0]
        assert session.idle_minutes == 22
        assert session.commits_ahead == 0  # a real 0, distinct from unknown
        assert session.last_activity_at == "2026-07-10T11:38:00+00:00"

    def test_session_hung_evidence_defaults_to_unknown_sentinels(self) -> None:
        """A session built without evidence carries the unknown sentinels, and
        those survive serialization (null activity round-trips as None)."""
        info = BoardSessionInfo(
            issue_number=1,
            issue_title="t",
            agent_type="",
            session_type="code",
            status="running",
            started_at="2026-07-10T11:30:00+00:00",
            age_minutes=1,
            terminal_id="issue-1",
        )
        assert (info.idle_minutes, info.commits_ahead, info.last_activity_at) == (
            -1,
            -1,
            None,
        )
        snapshot = BoardSnapshot(
            generated_at="2026-07-10T12:00:00+00:00",
            orchestrator_paused=False,
            sessions=[info],
        )

        recovered = BoardSnapshot.from_dict(snapshot.to_dict())

        assert recovered.sessions[0] == info

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        original = _sample_snapshot()
        path = tmp_path / "board" / "snapshot.json"  # parent dir must be created

        original.write(path)
        recovered = BoardSnapshot.read(path)

        assert recovered == original

    def test_e2e_health_round_trips(self) -> None:
        """The E2E health block survives the JSON boundary with nested runs."""
        recovered = BoardSnapshot.from_dict(_sample_snapshot().to_dict())

        health = recovered.e2e_health
        assert health is not None
        assert (health.enabled, health.stale, health.nonpassing_streak) == (True, True, 3)
        assert health.last_run is not None and health.last_run.id == 115
        assert [run.id for run in health.recent_runs] == [115, 114]
        assert health.recent_runs[1].duration_seconds is None  # None survives
        assert health.chronic_failures[0].tracking_issue == 6822
        assert health.chronic_failures[1].tracking_issue is None

    def test_absent_e2e_health_round_trips_as_none(self) -> None:
        """A snapshot with no E2E db serializes e2e_health as null and back."""
        original = BoardSnapshot(
            generated_at="2026-07-10T12:00:00+00:00",
            orchestrator_paused=False,
        )

        assert original.to_dict()["e2e_health"] is None
        recovered = BoardSnapshot.from_dict(original.to_dict())

        assert recovered.e2e_health is None
        assert recovered == original


class TestBoardSnapshotFailFast:
    def test_from_dict_rejects_wrong_schema_version(self) -> None:
        data = _sample_snapshot().to_dict()
        data["schema_version"] = BOARD_SNAPSHOT_SCHEMA_VERSION + 1

        with pytest.raises(ValueError, match="schema_version"):
            BoardSnapshot.from_dict(data)

    def test_from_dict_rejects_missing_schema_version(self) -> None:
        data = dict(_sample_snapshot().to_dict())
        del data["schema_version"]

        with pytest.raises(KeyError):
            BoardSnapshot.from_dict(data)  # type: ignore[arg-type]

    def test_from_dict_rejects_missing_required_key(self) -> None:
        data = dict(_sample_snapshot().to_dict())
        del data["generated_at"]

        with pytest.raises(KeyError):
            BoardSnapshot.from_dict(data)  # type: ignore[arg-type]

    def test_read_rejects_wrong_schema_version_from_file(self, tmp_path: Path) -> None:
        snapshot = _sample_snapshot()
        snapshot.schema_version = 999
        path = tmp_path / "snapshot.json"
        snapshot.write(path)

        with pytest.raises(ValueError, match="999"):
            BoardSnapshot.read(path)
