from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict

from .contracts import Event

@dataclass
class FakeEventStream:
    _queue: asyncio.Queue[Event]

    def __init__(self) -> None:
        self._queue = asyncio.Queue()

    def push(self, event: Event) -> None:
        self._queue.put_nowait(event)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while True:
            ev = await self._queue.get()
            if ev.get("type") == "__close__":
                return
            yield ev

@dataclass
class FakeSnapshotProvider:
    snapshot: Dict[str, Any]

    async def fetch_snapshot(self) -> Dict[str, Any]:
        import copy
        return copy.deepcopy(self.snapshot)
