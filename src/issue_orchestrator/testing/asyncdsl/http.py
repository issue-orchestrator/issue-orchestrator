from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

if TYPE_CHECKING:
    from http.client import HTTPResponse

from .contracts import Event

logger = logging.getLogger(__name__)


def _build_request(url: str, *, auth_token: str | None) -> urllib.request.Request:
    """Build a urllib Request, attaching ``Authorization: Bearer …`` when given.

    The orchestrator's ``/api/events``, ``/api/events_since`` and
    snapshot endpoints all require the loopback bearer token. Tests
    that bypass this (e.g. by passing a bare URL to ``urlopen``) get
    HTTP 401 and silently look like "the orchestrator isn't reachable".
    Centralizing the header injection here makes the auth contract
    explicit at the one place where the test client talks HTTP.
    """
    request = urllib.request.Request(url)
    if auth_token:
        request.add_header("Authorization", f"Bearer {auth_token}")
    return request


@dataclass
class SSEEventStream:
    url: str
    timeout_s: float = 30.0
    auth_token: str | None = field(default=None, repr=False)

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

    def _emit_event(self, event: dict[str, Any]) -> None:
        """Thread-safe emit of a parsed event to the queue."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _emit_close(self) -> None:
        """Thread-safe emit of close sentinel to the queue."""
        if self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, {"type": "__close__"})
            except RuntimeError:
                pass

    def _parse_sse_line(self, line: str, buffer: dict[str, list[str]]) -> bool:
        """Parse a single SSE line into the buffer.

        Returns True if an event was emitted, False otherwise.
        """
        if not line:
            # Empty line = end of event
            data_lines = buffer.get("data")
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    event = json.loads(payload)
                    # The control API sends complete event structure in data:
                    # {"event_id": N, "type": "...", "issue_key": "...", "payload": {...}}
                    self._emit_event(event)
                except json.JSONDecodeError:
                    pass
            buffer.clear()
            return True
        if line.startswith(":"):
            return False  # Comment line
        if line.startswith("data:"):
            buffer.setdefault("data", []).append(line[5:].lstrip())
        return False

    def _process_stream(self, resp: "HTTPResponse") -> None:
        """Process SSE lines from an open response stream."""
        buffer: dict[str, list[str]] = {}
        for raw in resp:
            if self._stop.is_set():
                break
            line = raw.decode("utf-8").rstrip("\r\n")
            self._parse_sse_line(line, buffer)

    def _handle_connection_error(self, exc: Exception | None, reason: str) -> bool:
        """Handle connection errors during SSE streaming.

        Returns True if we should break out of the main loop.
        """
        if self._stop.is_set():
            return True
        self._emit_reconnect(reason)
        if reason == "timeout":
            logger.info("[SSE] Stream idle timeout, reconnecting")
        elif reason == "error":
            logger.exception("[SSE] Stream error, reconnecting")
        return False

    def _run(self) -> None:
        if self._loop is None:
            return
        consecutive_failures = 0
        try:
            while not self._stop.is_set():
                try:
                    logger.info("[SSE] Connecting to %s", self.url)
                    request = _build_request(self.url, auth_token=self.auth_token)
                    with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                        self._process_stream(resp)
                    consecutive_failures = 0
                    self._emit_reconnect("closed")
                    logger.warning("[SSE] Stream closed, reconnecting")
                    # Brief pause before reconnecting after clean close
                    time.sleep(0.5)
                except TimeoutError:
                    if self._handle_connection_error(None, "timeout"):
                        break
                    consecutive_failures += 1
                except Exception:
                    if self._handle_connection_error(None, "error"):
                        break
                    consecutive_failures += 1
                # Exponential backoff on consecutive failures (cap at 30s)
                if consecutive_failures > 0:
                    backoff = min(0.5 * (2 ** (consecutive_failures - 1)), 30.0)
                    logger.info("[SSE] Backoff %.1fs before reconnect (failures=%d)", backoff, consecutive_failures)
                    # Use stop event for interruptible sleep
                    if self._stop.wait(backoff):
                        break
        finally:
            self._emit_close()


@dataclass
class HTTPSnapshotProvider:
    url: str
    auth_token: str | None = field(default=None, repr=False)

    async def fetch_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_sync)

    def _fetch_sync(self) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                request = _build_request(self.url, auth_token=self.auth_token)
                with urllib.request.urlopen(request, timeout=30.0) as resp:
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
    auth_token: str | None = field(default=None, repr=False)

    async def fetch_events_since(self, event_id: int) -> list[Event]:
        return await asyncio.to_thread(self._fetch_sync, event_id)

    def _fetch_sync(self, event_id: int) -> list[Event]:
        full_url = f"{self.url}?after={event_id}"
        request = _build_request(full_url, auth_token=self.auth_token)
        with urllib.request.urlopen(request, timeout=30.0) as resp:
            data = resp.read().decode("utf-8")
        payload = json.loads(data)
        return payload.get("events", [])
