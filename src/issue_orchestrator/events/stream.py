"""Event stream utilities for sequenced events and subscriptions."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque

from ..domain.issue_key import StableIssueId
from ..ports import EventSink, TraceEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StreamEvent:
    """Serializable event payload for streaming clients."""

    event_id: int
    type: str
    issue_key: StableIssueId | None
    payload: dict[str, Any]


@dataclass
class EventSubscription:
    """Represents a subscriber queue and its owning loop."""

    queue: asyncio.Queue[StreamEvent]
    loop: asyncio.AbstractEventLoop


class SequencedEventSink(EventSink):
    """Assigns monotonic event_id values before delegating to another sink."""

    def __init__(self, sink: EventSink, start_at: int = 1) -> None:
        self._sink = sink
        self._lock = threading.Lock()
        self._next_id = start_at

    def publish(self, event: TraceEvent) -> None:
        """Publish event with an assigned event_id."""
        if event.event_id is None:
            with self._lock:
                event_id = self._next_id
                self._next_id += 1
            event = event.with_event_id(event_id)
        self._sink.publish(event)


class EventHub(EventSink):
    """In-memory event hub with async subscriptions."""

    def __init__(
        self,
        max_events: int = 1000,
    ) -> None:
        self._events: Deque[StreamEvent] = deque(maxlen=max_events)
        self._subscribers: list[EventSubscription] = []
        self._lock = threading.Lock()
        self._last_event_id = 0
        self._total_published = 0
        self._total_skipped_no_id = 0
        self._total_replay_requests = 0
        self._total_replay_events = 0
        self._total_replay_misses = 0
        self._total_replay_out_of_range = 0

    @property
    def last_event_id(self) -> int:
        return self._last_event_id

    def buffer_range(self) -> tuple[int | None, int | None]:
        with self._lock:
            if not self._events:
                return None, None
            return self._events[0].event_id, self._events[-1].event_id

    def subscribe(self) -> EventSubscription:
        """Register a new subscriber queue."""
        queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=200)
        loop = asyncio.get_running_loop()
        sub = EventSubscription(queue=queue, loop=loop)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """Remove an existing subscriber queue."""
        with self._lock:
            try:
                self._subscribers.remove(subscription)
            except ValueError:
                pass

    def publish(self, event: TraceEvent) -> None:
        """Publish a stream event and fan it out to subscribers."""
        if event.event_id is None:
            logger.debug("EventHub skipping event without event_id: %s", event.name)
            with self._lock:
                self._total_skipped_no_id += 1
            return

        stream_event = self._to_stream_event(event)

        with self._lock:
            self._events.append(stream_event)
            self._last_event_id = max(self._last_event_id, stream_event.event_id)
            self._total_published += 1
            subscribers = list(self._subscribers)

        for sub in subscribers:
            try:
                sub.loop.call_soon_threadsafe(sub.queue.put_nowait, stream_event)
            except Exception:
                continue

    def get_since(self, event_id: int) -> list[StreamEvent]:
        """Return buffered events with id > event_id."""
        with self._lock:
            self._total_replay_requests += 1
            if not self._events:
                self._total_replay_misses += 1
                return []
            oldest_id = self._events[0].event_id
            if event_id < oldest_id:
                self._total_replay_out_of_range += 1
            events = [event for event in self._events if event.event_id > event_id]
            if not events:
                self._total_replay_misses += 1
                return []
            self._total_replay_events += len(events)
            return events

    def stats(self) -> dict[str, int | None]:
        oldest_id, newest_id = self.buffer_range()
        with self._lock:
            return {
                "buffer_size": len(self._events),
                "buffer_max": self._events.maxlen,
                "oldest_event_id": oldest_id,
                "newest_event_id": newest_id,
                "last_event_id": self._last_event_id,
                "subscribers": len(self._subscribers),
                "total_published": self._total_published,
                "total_skipped_no_id": self._total_skipped_no_id,
                "total_replay_requests": self._total_replay_requests,
                "total_replay_events": self._total_replay_events,
                "total_replay_misses": self._total_replay_misses,
                "total_replay_out_of_range": self._total_replay_out_of_range,
            }

    def _to_stream_event(self, event: TraceEvent) -> StreamEvent:
        raw_key = event.data.get("issue_key")
        if raw_key is not None:
            issue_key: StableIssueId | None = StableIssueId(raw_key)
        else:
            issue_number = event.data.get("issue_number")
            if issue_number is not None:
                issue_key = StableIssueId(str(issue_number))
            else:
                issue_key = None

        event_type = _map_event_type(event.name)
        return StreamEvent(
            event_id=event.event_id or 0,
            type=event_type,
            issue_key=issue_key,
            payload=dict(event.data),
        )


def _map_event_type(name: str) -> str:
    """Pass through event type unchanged.

    Event names use dot notation (e.g., 'session.completed') as defined in
    EventName constants. Clients receive the same names - no transformation.
    This ensures a common language across server and clients.
    """
    return name
