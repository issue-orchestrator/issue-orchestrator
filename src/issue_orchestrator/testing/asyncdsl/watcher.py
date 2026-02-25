from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Optional

from issue_orchestrator.domain.issue_key import StableIssueId
from .contracts import EventStream, SnapshotProvider, ReplayProvider
from .materializer import MaterializedView
from .dsl import IssueWatch, SystemWatch
from .config import WatcherConfig
from .errors import EventGapDetected

logger = logging.getLogger(__name__)
@dataclass
class OrchestratorWatcher:
    cfg: WatcherConfig
    view: MaterializedView
    _notify: asyncio.Event
    _consumer_task: asyncio.Task[None] | None
    _snapshot_provider: SnapshotProvider
    _replay_provider: ReplayProvider | None

    @classmethod
    async def create(
        cls,
        *,
        event_stream: EventStream,
        snapshot_provider: SnapshotProvider,
        replay_provider: ReplayProvider | None = None,
        config: Optional[WatcherConfig] = None,
    ) -> "OrchestratorWatcher":
        cfg = config or WatcherConfig()
        view = MaterializedView()
        view.set_diag_limits(cfg.diag_max_events)
        notify = asyncio.Event()

        self = cls(
            cfg=cfg,
            view=view,
            _notify=notify,
            _consumer_task=None,
            _snapshot_provider=snapshot_provider,
            _replay_provider=replay_provider,
        )

        raw = await snapshot_provider.fetch_snapshot()
        view.apply_snapshot(raw)
        notify.set()

        self._consumer_task = asyncio.create_task(self._consume(event_stream))
        return self

    async def close(self) -> None:
        if self._consumer_task is None:
            return
        self._consumer_task.cancel()
        try:
            await self._consumer_task
        except asyncio.CancelledError:
            pass

    def issue(self, issue_key: StableIssueId) -> IssueWatch:
        return IssueWatch(issue_key=issue_key, view=self.view, cfg=self.cfg, _notify=self._notify)

    def system(self) -> SystemWatch:
        return SystemWatch(view=self.view, cfg=self.cfg, _notify=self._notify)

    async def resync_snapshot(self) -> None:
        raw = await self._snapshot_provider.fetch_snapshot()
        self.view.apply_snapshot(raw)
        self._notify.set()

    async def _consume(self, event_stream: EventStream) -> None:
        async for event in event_stream:
            if event.get("type") == "__reconnect__":
                logger.info(
                    "[SSE] Reconnect event received (reason=%s), attempting replay",
                    event.get("reason"),
                )
                if await self._try_replay_gap():
                    logger.info("[SSE] Replay succeeded from event_id=%s", self.view.last_event_id)
                else:
                    logger.info("[SSE] Replay returned no events after reconnect")
                    if self.cfg.resync_on_gap:
                        logger.info("[SSE] Resyncing snapshot after reconnect")
                        await self.resync_snapshot()
                continue
            try:
                self.view.apply_event(event, gap_check=True)
                self._notify.set()
            except EventGapDetected:
                logger.warning(
                    "[SSE] Gap detected at event_id=%s, attempting replay",
                    self.view.last_event_id,
                )
                if await self._try_replay_gap():
                    logger.info("[SSE] Replay succeeded from event_id=%s", self.view.last_event_id)
                    continue
                logger.warning("[SSE] Replay failed, resync_on_gap=%s", self.cfg.resync_on_gap)
                if not self.cfg.resync_on_gap:
                    raise
                await self.resync_snapshot()

    async def _try_replay_gap(self) -> bool:
        if self._replay_provider is None:
            return False
        try:
            events = await self._replay_provider.fetch_events_since(self.view.last_event_id)
        except Exception:
            logger.exception("[SSE] Replay fetch failed for event_id=%s", self.view.last_event_id)
            return False
        if not events:
            logger.info("[SSE] Replay returned 0 events for event_id=%s", self.view.last_event_id)
            return False
        try:
            for event in sorted(events, key=lambda ev: ev.get("event_id", 0)):
                self.view.apply_event(event, gap_check=True)
                self._notify.set()
        except EventGapDetected:
            logger.warning("[SSE] Replay still has gaps at event_id=%s", self.view.last_event_id)
            return False
        logger.info("[SSE] Replay applied %d events", len(events))
        return True
