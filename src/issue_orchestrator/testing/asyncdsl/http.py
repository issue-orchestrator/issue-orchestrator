from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any, AsyncIterator

from .contracts import Event

logger = logging.getLogger(__name__)


@dataclass
class SSEEventStream:
    url: str
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    async def close(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 1.0)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[Event]:
        while True:
            event = await self._queue.get()
            if event.get("type") == "__close__":
                return
            yield event

    def _emit_reconnect(self, reason: str) -> None:
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(
                    self._queue.put_nowait,
                    {"type": "__reconnect__", "reason": reason, "ts": time.time()},
                )
            except RuntimeError:
                pass

    def _run(self) -> None:
        if self._loop is None:
            return
        try:
            while not self._stop.is_set():
                try:
                    logger.info("[SSE] Connecting to %s", self.url)
                    with urllib.request.urlopen(self.url, timeout=self.timeout_s) as resp:
                        buffer: dict[str, list[str]] = {}
                        for raw in resp:
                            if self._stop.is_set():
                                break
                            line = raw.decode("utf-8").rstrip("\r\n")
                            if not line:
                                data_lines = buffer.get("data")
                                if data_lines:
                                    payload = "\n".join(data_lines)
                                    try:
                                        event = json.loads(payload)
                                    except json.JSONDecodeError:
                                        buffer = {}
                                        continue
                                    # The control API sends complete event structure in data:
                                    # {"event_id": N, "type": "...", "issue_key": "...", "payload": {...}}
                                    self._loop.call_soon_threadsafe(self._queue.put_nowait, event)
                                buffer = {}
                                continue
                            if line.startswith(":"):
                                continue
                            if line.startswith("data:"):
                                buffer.setdefault("data", []).append(line[5:].lstrip())
                    self._emit_reconnect("closed")
                    logger.warning("[SSE] Stream closed, reconnecting")
                except TimeoutError:
                    # SSE streams can be idle; retry to keep the stream alive.
                    self._emit_reconnect("timeout")
                    logger.info("[SSE] Stream idle timeout, reconnecting")
                    continue
                except Exception:
                    # Transient network errors - retry unless stopping.
                    if self._stop.is_set():
                        break
                    self._emit_reconnect("error")
                    logger.exception("[SSE] Stream error, reconnecting")
        finally:
            if self._loop and not self._loop.is_closed():
                try:
                    self._loop.call_soon_threadsafe(self._queue.put_nowait, {"type": "__close__"})
                except RuntimeError:
                    pass


@dataclass
class HTTPSnapshotProvider:
    url: str

    async def fetch_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(self.url, timeout=30.0) as resp:
                    data = resp.read().decode("utf-8")
                return json.loads(data)
            except (TimeoutError, urllib.error.URLError) as exc:
                last_exc = exc
                if attempt < 2:
                    backoff_s = 1 + attempt
                    logger.warning(
                        "Snapshot fetch failed (attempt %d/3): %s. Retrying in %ds",
                        attempt + 1,
                        exc,
                        backoff_s,
                    )
                    time.sleep(backoff_s)
        if last_exc:
            raise last_exc
        raise RuntimeError("Snapshot fetch failed for unknown reasons")


@dataclass
class HTTPReplayProvider:
    url: str

    async def fetch_events_since(self, event_id: int) -> list[Event]:
        return await asyncio.to_thread(self._fetch_sync, event_id)

    def _fetch_sync(self, event_id: int) -> list[Event]:
        full_url = f"{self.url}?after={event_id}"
        with urllib.request.urlopen(full_url, timeout=30.0) as resp:
            data = resp.read().decode("utf-8")
        payload = json.loads(data)
        return payload.get("events", [])
