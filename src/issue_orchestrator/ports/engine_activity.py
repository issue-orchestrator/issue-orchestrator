"""Which Repository Engines are running, and what they have been doing (#7567)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from ..domain.engine_activity import EngineActivityObservation, EngineRef


class EngineInventory(Protocol):
    def engines(self, *, now: datetime, recent: timedelta) -> tuple[EngineRef, ...]:
        """Every engine Control Center registers that is running, or ran
        within ``recent`` of ``now``, once each."""
        ...


class EngineActivityProbe(Protocol):
    def observe(self, *, now: datetime, recent: timedelta) -> EngineActivityObservation:
        """Each in-scope engine's activity watermark, read from byte copies of
        its state (never its live databases, never GitHub)."""
        ...


__all__ = ["EngineActivityProbe", "EngineInventory"]
