"""Round-trip and fail-fast tests for the BoardSnapshot domain type.

The snapshot crosses a JSON file boundary between the orchestrator and a
triage/tech-lead agent session, so these tests exercise both directions
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

    def test_write_read_round_trip(self, tmp_path: Path) -> None:
        original = _sample_snapshot()
        path = tmp_path / "board" / "snapshot.json"  # parent dir must be created

        original.write(path)
        recovered = BoardSnapshot.read(path)

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
