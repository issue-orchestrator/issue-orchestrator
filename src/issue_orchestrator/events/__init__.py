"""Event catalog and context for structured event emission.

This module provides:
- EventName: Canonical event name constants
- EventContext: Run/tick context for event payloads
- Helpers for building consistent event payloads
"""

from .catalog import EventName
from .context import EventContext
from .stream import EventHub, SequencedEventSink, StreamEvent, EventSubscription

__all__ = [
    "EventName",
    "EventContext",
    "EventHub",
    "SequencedEventSink",
    "StreamEvent",
    "EventSubscription",
]
