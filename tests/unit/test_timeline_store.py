"""Unit tests for timeline storage and writer adapters."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.execution.timeline_store import FileSystemTimelineStore, TimelineStoreConfig
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

    records = store.read(issue)
    assert [record.event_id for record in records] == ["2", "3"]


def test_timeline_store_read_limit_returns_tail(tmp_path: Path) -> None:
    store = FileSystemTimelineStore(tmp_path, config=TimelineStoreConfig(max_records=10))
    issue = 7
    store.append(issue, TimelineRecord(event_id="a", timestamp="t1", event="e1", data={}))
    store.append(issue, TimelineRecord(event_id="b", timestamp="t2", event="e2", data={}))
    store.append(issue, TimelineRecord(event_id="c", timestamp="t3", event="e3", data={}))

    records = store.read(issue, limit=1)
    assert len(records) == 1
    assert records[0].event_id == "c"


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
