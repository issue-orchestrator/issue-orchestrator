"""Unit tests for timeline storage and writer adapters."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from issue_orchestrator.execution.timeline_store import (
    FileSystemTimelineStore,
    RoutedTimelineStore,
    SqliteTimelineStore,
    TimelineStoreConfig,
)
from issue_orchestrator.execution.timeline_writer import DefaultTimelineWriter
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.timeline_store import TimelineRecord, TimelineStore
from issue_orchestrator.events import EventName


class RecordingTimelineStore(TimelineStore):
    def __init__(self) -> None:
        self.records: list[TimelineRecord] = []

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        self.records.append(record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        return self.records


def test_timeline_store_trims_max_records(tmp_path: Path) -> None:
    store = FileSystemTimelineStore(tmp_path, config=TimelineStoreConfig(max_records=2))
    issue = 42
    store.append(issue, TimelineRecord(event_id="1", timestamp="t1", event="e1", data={}))
    store.append(issue, TimelineRecord(event_id="2", timestamp="t2", event="e2", data={}))
    store.append(issue, TimelineRecord(event_id="3", timestamp="t3", event="e3", data={}))
    store.append(issue, TimelineRecord(event_id="4", timestamp="t4", event="e4", data={}))
    store.append(issue, TimelineRecord(event_id="5", timestamp="t5", event="e5", data={}))

    records = store.read(issue)
    assert [record.event_id for record in records] == ["4", "5"]


def test_timeline_store_read_limit_returns_tail(tmp_path: Path) -> None:
    store = FileSystemTimelineStore(tmp_path, config=TimelineStoreConfig(max_records=10))
    issue = 7
    store.append(issue, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={}))
    store.append(issue, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={}))
    store.append(issue, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={}))

    records = store.read(issue, limit=1)
    assert len(records) == 1
    assert records[0].event_id == "c"


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


def test_filesystem_and_sqlite_store_roundtrip_equivalence(tmp_path: Path) -> None:
    fs_store = FileSystemTimelineStore(
        tmp_path / "fs-root",
        config=TimelineStoreConfig(max_records=50),
    )
    sqlite_store = SqliteTimelineStore(
        tmp_path / "sqlite-root" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=50, max_total_records=500),
    )
    records = [
        TimelineRecord(
            event_id="e1",
            timestamp="2026-02-17T10:00:00Z",
            event="session.started",
            data={"issue_number": 4057, "run_dir": "/tmp/r1", "task": "code", "schema": 1},
        ),
        TimelineRecord(
            event_id="e2",
            timestamp="2026-02-17T10:05:00Z",
            event="review.started",
            data={"issue_number": 4057, "run_dir": "/tmp/r2", "task": "review", "schema": 1},
        ),
        TimelineRecord(
            event_id="e3",
            timestamp="2026-02-17T10:10:00Z",
            event="review.approved",
            data={"issue_number": 4057, "summary": "looks good", "schema": 1},
        ),
    ]
    for record in records:
        fs_store.append(4057, record)
        sqlite_store.append(4057, record)

    assert fs_store.read(4057) == sqlite_store.read(4057)
    assert fs_store.read(4057, limit=2) == sqlite_store.read(4057, limit=2)


def test_filesystem_and_sqlite_writer_equivalence_for_all_event_names(tmp_path: Path) -> None:
    issue_number = 4057
    fs_store = FileSystemTimelineStore(
        tmp_path / "fs-root",
        config=TimelineStoreConfig(max_records=10000),
    )
    sqlite_store = SqliteTimelineStore(
        tmp_path / "sqlite-root" / "timeline.sqlite",
        config=TimelineStoreConfig(max_records=10000, max_total_records=20000),
    )
    fs_writer = DefaultTimelineWriter(fs_store)
    sqlite_writer = DefaultTimelineWriter(sqlite_store)

    base_time = datetime(2026, 2, 17, 12, 0, 0, tzinfo=timezone.utc)
    for idx, event_name in enumerate(EventName, start=1):
        payload = {
            "issue_number": issue_number,
            "run_id": f"run-{idx}",
            "run_dir": f"/tmp/run-{idx}",
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
        fs_writer.record(event)
        sqlite_writer.record(event)

    fs_records = fs_store.read(issue_number)
    sqlite_records = sqlite_store.read(issue_number)
    assert fs_records == sqlite_records


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


def test_timeline_writer_preserves_sequenced_event_id_and_schema() -> None:
    store = RecordingTimelineStore()
    writer = DefaultTimelineWriter(store)
    event = TraceEvent(
        EventName.SESSION_STARTED,
        {"issue_number": 4057, "task": "code"},
        event_id=77,
    )

    writer.record(event)
    assert len(store.records) == 1
    record = store.records[0]
    assert record.event_id == "77"
    assert record.data["schema"] == 1
    assert record.data["timeline_schema_version"] == 2


def test_routed_timeline_store_routes_by_worktree_path(tmp_path: Path) -> None:
    default_root = tmp_path / "repo-root"
    issue_root = tmp_path / "issue-4057-worktree"
    default_root.mkdir(parents=True)
    issue_root.mkdir(parents=True)
    store = RoutedTimelineStore(default_root, TimelineStoreConfig(max_records=10))

    record = TimelineRecord(
        event_id="r1",
        timestamp="t1",
        event="session.started",
        data={"issue_number": 4057, "worktree_path": str(issue_root)},
    )
    store.append(4057, record)

    default_timeline = default_root / ".issue-orchestrator" / "state" / "timeline" / "issue-4057.jsonl"
    issue_timeline = issue_root / ".issue-orchestrator" / "state" / "timeline" / "issue-4057.jsonl"
    assert not default_timeline.exists()
    assert issue_timeline.exists()
    assert store.owner_repo_root(4057) == issue_root.resolve()
    assert len(store.read(4057)) == 1


def test_routed_timeline_store_routes_by_run_dir(tmp_path: Path) -> None:
    default_root = tmp_path / "repo-root"
    issue_root = tmp_path / "issue-4072-worktree"
    run_dir = issue_root / ".issue-orchestrator" / "sessions" / "20260216-100000Z__issue-4072"
    run_dir.mkdir(parents=True)
    default_root.mkdir(parents=True)
    store = RoutedTimelineStore(default_root, TimelineStoreConfig(max_records=10))

    store.append(
        4072,
        TimelineRecord(
            event_id="r2",
            timestamp="t2",
            event="session.completed",
            data={"issue_number": 4072, "run_dir": str(run_dir)},
        ),
    )

    issue_timeline = issue_root / ".issue-orchestrator" / "state" / "timeline" / "issue-4072.jsonl"
    assert issue_timeline.exists()
    assert store.owner_repo_root(4072) == issue_root.resolve()
