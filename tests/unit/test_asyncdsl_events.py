from __future__ import annotations

import asyncio

import pytest

from issue_orchestrator.events import EventName
from issue_orchestrator.testing.asyncdsl.config import WatcherConfig
from issue_orchestrator.testing.asyncdsl.dsl import IssueWatch, SystemWatch
from issue_orchestrator.testing.asyncdsl.materializer import MaterializedView


@pytest.mark.asyncio
async def test_system_watch_event_matches_payload() -> None:
    view = MaterializedView()
    notify = asyncio.Event()
    cfg = WatcherConfig()
    watch = SystemWatch(view=view, cfg=cfg, _notify=notify)

    async def emit() -> None:
        await asyncio.sleep(0.01)
        view.apply_event({
            "event_id": 1,
            "type": EventName.GH_SEARCH_ITEM_MALFORMED.value,
            "payload": {"label": "test-label"},
        })
        notify.set()

    task = asyncio.create_task(emit())
    await watch.event(
        EventName.GH_SEARCH_ITEM_MALFORMED,
        predicate=lambda e: e.get("payload", {}).get("label") == "test-label",
        timeout_s=1.0,
    )
    await task


@pytest.mark.asyncio
async def test_issue_watch_event_matches_issue_key() -> None:
    view = MaterializedView()
    notify = asyncio.Event()
    cfg = WatcherConfig()
    watch = IssueWatch(issue_key="123", view=view, cfg=cfg, _notify=notify)

    async def emit() -> None:
        await asyncio.sleep(0.01)
        view.apply_event({
            "event_id": 1,
            "type": EventName.ISSUE_LABELS_CHANGED.value,
            "issue_key": "123",
            "payload": {"labels": ["in-progress"]},
        })
        notify.set()

    task = asyncio.create_task(emit())
    await watch.event(
        EventName.ISSUE_LABELS_CHANGED,
        predicate=lambda e: "in-progress" in e.get("payload", {}).get("labels", []),
        timeout_s=1.0,
    )
    await task
