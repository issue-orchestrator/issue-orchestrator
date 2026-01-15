from __future__ import annotations

import asyncio

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher
from tests.e2e.flows import E2EFlow


class _QueueEventStream:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def __aiter__(self) -> "_QueueEventStream":
        return self

    async def __anext__(self) -> dict:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def push(self, event: dict) -> None:
        await self._queue.put(event)

    async def close(self) -> None:
        await self._queue.put(None)


class _SnapshotProvider:
    url = "http://localhost:0/api/snapshot"

    async def fetch_snapshot(self) -> dict:
        return {
            "snapshot_id": 0,
            "orchestrator": {},
            "issues": {},
        }


@pytest.mark.asyncio
async def test_flow_event_waits_for_system_event(monkeypatch) -> None:
    monkeypatch.setenv("E2E_CONTROL_API_PORT", "0")
    stream = _QueueEventStream()
    watcher = await OrchestratorWatcher.create(
        event_stream=stream,
        snapshot_provider=_SnapshotProvider(),
        replay_provider=None,
    )
    flow = E2EFlow(repo="owner/repo", watcher=watcher)

    async def emit() -> None:
        await asyncio.sleep(0.01)
        await stream.push({
            "event_id": 1,
            "type": EventName.GH_SEARCH_ITEM_MALFORMED.value,
            "payload": {"label": "test-label"},
        })
        await stream.close()

    task = asyncio.create_task(emit())
    await flow.event(
        EventName.GH_SEARCH_ITEM_MALFORMED,
        predicate=lambda e: e.get("payload", {}).get("label") == "test-label",
        timeout_s=1.0,
    )
    await watcher.close()
    await task
