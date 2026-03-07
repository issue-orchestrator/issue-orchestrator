"""Unit tests for timeline storage and writer adapters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import pytest

from issue_orchestrator.execution.timeline_store import (
    SqliteTimelineStore,
    TimelineStoreConfig,
)
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
from issue_orchestrator.events import EventName
from issue_orchestrator.events.catalog import EVENT_SCHEMA_VERSION
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION


class RecordingTimelineStore(TimelineStore):
    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        self.records.append(record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        return self.records


def test_sqlite_timeline_store_trims_max_records(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=2),
    )
    issue = 42
    store.append(issue, TimelineRecord(event_id="1", timestamp="t1", event="e1", data={}))
    store.append(issue, TimelineRecord(event_id="2", timestamp="t2", event="e2", data={}))
    store.append(issue, TimelineRecord(event_id="3", timestamp="t3", event="e3", data={}))

    records = store.read(issue)
    assert [record.event_id for record in records] == ["2", "3"]


def test_sqlite_timeline_store_read_limit_returns_tail(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    issue = 7
    store.append(issue, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={}))
    store.append(issue, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={}))
    store.append(issue, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={}))

    records = store.read(issue, limit=1)
    assert len(records) == 1
    assert records[0].event_id == "c"


def test_sqlite_timeline_store_trims_total_records_across_issues(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=100, max_total_records=3),
    )
    store.append(1, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={}))
    store.append(2, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={}))
    store.append(1, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={}))
    store.append(2, TimelineRecord(event_id="d", timestamp="t4", event="e4", data={}))

    assert [record.event_id for record in store.read(1)] == ["c"]
    assert [record.event_id for record in store.read(2)] == ["b", "d"]


def test_sqlite_timeline_store_preserves_run_dir_payload(tmp_path: Path) -> None:
    run_dir = (
        tmp_path
        / "wt-4057"
        / ".issue-orchestrator"
        / "sessions"
        / "20260217-000001Z__issue-4057"
    )
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        4057,
        TimelineRecord(
            event_id="run-event",
            timestamp="2026-02-17T00:00:01Z",
            event="session.started",
            data={"issue_number": 4057, "run_dir": str(run_dir), "task": "code"},
        ),
    )

    records = store.read(4057)
    assert len(records) == 1
    assert records[0].data["run_dir"] == str(run_dir)
    assert records[0].data["task"] == "code"


def test_sqlite_timeline_store_ignores_legacy_jsonl_files(tmp_path: Path) -> None:
    legacy_dir = tmp_path / "timeline"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "issue-4057.jsonl"
    legacy_records = [
        {
            "event_id": "legacy-1",
            "timestamp": "2026-02-17T00:00:01Z",
            "event": "session.started",
            "data": {"issue_number": 4057, "run_dir": "/tmp/run-1"},
        },
        {
            "event_id": "legacy-2",
            "timestamp": "2026-02-17T00:10:01Z",
            "event": "session.completed",
            "data": {"issue_number": 4057},
        },
    ]
    legacy_path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in legacy_records) + "\n",
        encoding="utf-8",
    )

    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    # Forward-only policy: legacy JSONL is ignored by the SQLite store.
    records = store.read(4057)
    assert records == []


def test_sqlite_timeline_store_fails_fast_if_db_file_is_replaced(tmp_path: Path) -> None:
    db_path = tmp_path / "timeline.sqlite"
    store = SqliteTimelineStore(
        db_path,
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(
            event_id="a",
            timestamp="t1",
            event="session.started",
            data={"issue_number": 42, "run_dir": "/tmp/run-42"},
        ),
    )

    replacement = tmp_path / "replacement.sqlite"
    replacement.write_text("", encoding="utf-8")
    os.replace(replacement, db_path)

    with pytest.raises(RuntimeError, match="replaced on disk"):
        store.read(42)


def test_sqlite_timeline_store_requires_run_dir_for_run_scoped_events(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )

    with pytest.raises(RuntimeError, match="requires non-empty run_dir"):
        store.append(
            42,
            TimelineRecord(
                event_id="missing-run-dir",
                timestamp="t1",
                event="session.started",
                data={"issue_number": 42},
            ),
        )


def test_sqlite_writer_covers_all_event_names(tmp_path: Path) -> None:
    issue_number = 4057
    sqlite_store = SqliteTimelineStore(
        tmp_path / "sqlite-root" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10000, max_total_records=20000),
    )
    sqlite_writer = DefaultTimelineWriter(sqlite_store)

    base_time = datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc)
    for idx, event_name in enumerate(EventName, start=1):
        run_dir = tmp_path / "runs" / f"run-{idx}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "ui-session.log").write_text("log\n", encoding="utf-8")
        completion_path = run_dir / "completion-agent_backend.json"
        completion_path.write_text('{"status":"completed"}\n', encoding="utf-8")
        payload = {
            "issue_number": issue_number,
            "run_id": f"run-{idx}",
            "run_dir": str(run_dir),
            "completion_path_absolute": str(completion_path),
            "agent": "agent:backend",
            "task": "review" if "review" in event_name.value else "code",
            "rework_cycle": idx % 3,
            "reviewer_agent": "agent:reviewer",
            "added": ["label-a", "label-b"],
            "removed": ["label-c"],
            "summary": f"summary-{idx}",
            "metadata": {"event_index": idx, "event_name": event_name.value},
            "path_value": Path(f"/tmp/path-{idx}"),
            "when": base_time + timedelta(seconds=idx),
        }
        event = TraceEvent(
            event_name,
            payload,
            event_id=idx,
            timestamp=base_time + timedelta(minutes=idx),
        )
        sqlite_writer.record(event)

    sqlite_records = sqlite_store.read(issue_number)
    assert len(sqlite_records) == len(EventName)
    for record in sqlite_records:
        assert record.data["schema"] == EVENT_SCHEMA_VERSION
        assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION


def test_timeline_writer_normalizes_non_json_types() -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    event = TraceEvent(
        EventName.TICK_STARTED,
        {
            "issue_number": 1,
            "path": Path("/tmp/example"),
            "values": {"a", "b"},
            "when": datetime(2026, 2, 6, 12, 0, 0, tzinfo=timezone.utc),
        },
    )

    writer.record(event)
    assert store.records
    data = store.records[0].data
    assert data["path"] == "/tmp/example"
    assert sorted(data["values"]) == ["a", "b"]
    assert data["when"].startswith("2026-02-06T12:00:00")


def test_timeline_writer_preserves_sequenced_event_id_and_schema(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r77__issue-4057"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {"issue_number": 4057, "task": "code", "run_dir": str(run_dir)},
        event_id=77,
    )

    writer.record(event)
    assert len(store.records) == 1
    record = store.records[0]
    assert record.event_id == "77"
    assert record.data["schema"] == EVENT_SCHEMA_VERSION
    assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION
    assert record.data["event_intent"] == "coding"
    assert record.data["logical_run"] == 1
    assert record.data["logical_cycle"] == 1
    assert record.data["logical_phase"] == "coding"


def test_timeline_writer_overwrites_stale_schema_versions(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r88__issue-4057"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {
            "issue_number": 4057,
            "task": "code",
            "run_dir": str(run_dir),
            "schema": -1,
            "timeline_schema_version": -1,
        },
        event_id=88,
    )

    writer.record(event)
    assert len(store.records) == 1
    record = store.records[0]
    assert record.data["schema"] == EVENT_SCHEMA_VERSION
    assert record.data["timeline_schema_version"] == TIMELINE_SCHEMA_VERSION


def test_timeline_writer_requires_session_log_for_session_started(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "r1__issue-4057"
    run_dir.mkdir(parents=True)
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {"issue_number": 4057, "run_dir": str(run_dir), "task": "code"},
    )
    with pytest.raises(RuntimeError, match="session_log_missing"):
        writer.record(event)

    (run_dir / "ui-session.log").write_text("", encoding="utf-8")
    writer.record(event)
    assert len(store.records) == 1


def test_timeline_writer_requires_completion_record_for_session_completed(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    completion = tmp_path / "completion.json"
    event = TraceEvent(
        EventName.SESSION_COMPLETED,
        {"issue_number": 4057, "completion_path_absolute": str(completion), "task": "code"},
    )
    with pytest.raises(RuntimeError, match="missing_path"):
        writer.record(event)

    completion.write_text('{"status":"completed"}\n', encoding="utf-8")
    writer.record(event)
    assert len(store.records) == 1


def test_timeline_writer_requires_review_feedback_reference_for_review_comment_added() -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    event = TraceEvent(
        EventName.REVIEW_COMMENT_ADDED,
        {"issue_number": 4057, "comment_url": "", "task": "review"},
    )
    with pytest.raises(RuntimeError, match="missing_review_feedback_reference"):
        writer.record(event)

    writer.record(
        TraceEvent(
            EventName.REVIEW_COMMENT_ADDED,
            {
                "issue_number": 4057,
                "comment_url": "https://github.com/org/repo/pull/4124#discussion_r1",
                "task": "review",
            },
        )
    )
    assert len(store.records) == 1


# ---------------------------------------------------------------------------
# source_event round-trip through SQLite
# ---------------------------------------------------------------------------


def test_sqlite_store_persists_source_event(tmp_path: Path) -> None:
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(
            event_id="fan-1",
            timestamp="t1",
            event="agent.coding_started",
            data={"issue_number": 42, "run_dir": "/tmp/run-42"},
            source_event="session.started",
        ),
    )

    records = store.read(42)
    assert len(records) == 1
    assert records[0].event == "agent.coding_started"
    assert records[0].source_event == "session.started"


def test_sqlite_store_source_event_defaults_to_event_name(tmp_path: Path) -> None:
    """When source_event is not set, it defaults to the event name."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    store.append(
        42,
        TimelineRecord(event_id="no-source", timestamp="t1", event="e1", data={}),
    )

    records = store.read(42)
    assert records[0].source_event == "e1"


def test_sqlite_store_run_dir_check_uses_source_event(tmp_path: Path) -> None:
    """The CHECK constraint should use source_event for run_dir validation."""
    store = SqliteTimelineStore(
        tmp_path / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10),
    )
    # Fan-out event: external name is "agent.coding_started" but source is "session.started"
    # which requires run_dir. Should fail without run_dir.
    with pytest.raises(RuntimeError, match="requires non-empty run_dir"):
        store.append(
            42,
            TimelineRecord(
                event_id="bad",
                timestamp="t1",
                event="agent.coding_started",
                data={"issue_number": 42},
                source_event="session.started",
            ),
        )


def test_writer_fan_out_records_have_views_tags(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("log")
    writer.record(
        TraceEvent(
            EventName.SESSION_STARTED,
            {"issue_number": 42, "run_dir": str(run_dir), "task": "code"},
        )
    )

    assert len(store.records) >= 1
    for record in store.records:
        assert "views" in record.data
        assert isinstance(record.data["views"], list)
        assert record.source_event == "session.started"


def test_writer_fan_out_narrative_stored_in_data(tmp_path: Path) -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    run_dir = tmp_path / "sessions" / "run1"
    run_dir.mkdir(parents=True)
    (run_dir / "ui-session.log").write_text("log")
    writer.record(
        TraceEvent(
            EventName.SESSION_STARTED,
            {"issue_number": 42, "run_dir": str(run_dir), "task": "code"},
        )
    )

    user_record = next(r for r in store.records if r.event == "agent.coding_started")
    assert user_record.data["narrative"] == "Coding agent started"

