from __future__ import annotations
from typing import AsyncIterator, Protocol, Any, Dict

Event = Dict[str, Any]

class EventStream(Protocol):
    def __aiter__(self) -> AsyncIterator[Event]: ...

class SnapshotProvider(Protocol):
    async def fetch_snapshot(self) -> dict[str, Any]: ...


class ReplayProvider(Protocol):
    async def fetch_events_since(self, event_id: int) -> list[Event]: ...
