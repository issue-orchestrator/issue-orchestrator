"""Which Repository Engines are running, and what they have been doing (#7567)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..domain.engine_activity import EngineActivityObservation, EngineSighting


class EngineInventory(Protocol):
    def engines(self, *, since: datetime) -> tuple[EngineSighting, ...]:
        """Every engine Control Center registers that is running, or wrote its
        log at or after ``since``, once each."""
        ...


class EngineActivityProbe(Protocol):
    def observe(self, *, now: datetime, since: datetime) -> EngineActivityObservation:
        """Each in-scope engine's activity watermark, read from byte copies of
        its state (never its live databases, never GitHub)."""
        ...


__all__ = ["EngineActivityProbe", "EngineInventory"]
