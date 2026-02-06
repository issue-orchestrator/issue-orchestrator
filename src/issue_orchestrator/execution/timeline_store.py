"""Filesystem-backed timeline store."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import deque
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from ..infra.repo_identity import state_dir
from ..ports.timeline_store import TimelineRecord, TimelineStore


@dataclass(frozen=True)
class TimelineStoreConfig:
    max_records: int = 5000


class FileSystemTimelineStore(TimelineStore):
    """Append-only JSONL timeline store per issue."""

    def __init__(self, repo_root: Path, config: TimelineStoreConfig | None = None):
        self._root = state_dir(repo_root) / "timeline"
        self._root.mkdir(parents=True, exist_ok=True)
        self._config = config or TimelineStoreConfig()

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        path = self._issue_path(issue_number)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.__dict__, sort_keys=True, default=str) + "\n")
        self._trim_if_needed(path)

    def append_event(self, issue_number: int, event: str, data: dict) -> None:
        record = TimelineRecord(
            event_id=str(uuid4()),
            timestamp=_now_iso(),
            event=event,
            data=data,
        )
        self.append(issue_number, record)

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        path = self._issue_path(issue_number)
        if not path.exists():
            return []
        return list(_load_records(path, limit=limit))

    def _issue_path(self, issue_number: int) -> Path:
        return self._root / f"issue-{issue_number}.jsonl"

    def _trim_if_needed(self, path: Path) -> None:
        max_records = self._config.max_records
        if max_records <= 0 or not path.exists():
            return
        buffer: deque[str] = deque(maxlen=max_records)
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                count += 1
                buffer.append(line)
        if count <= max_records:
            return
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(buffer)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_records(path: Path, limit: int | None = None) -> Iterable[TimelineRecord]:
    lines: Iterable[str]
    if limit is not None and limit > 0:
        buffer: deque[str] = deque(maxlen=limit)
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    buffer.append(line)
        lines = list(buffer)
    else:
        with path.open("r", encoding="utf-8") as handle:
            lines = [line for line in handle if line.strip()]
    for line in lines:
        payload = json.loads(line)
        yield TimelineRecord(
            event_id=payload.get("event_id", ""),
            timestamp=payload.get("timestamp", ""),
            event=payload.get("event", ""),
            data=payload.get("data") or {},
        )
