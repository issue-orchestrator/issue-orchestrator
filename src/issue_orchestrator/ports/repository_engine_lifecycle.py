"""Ports for exact Repository Engine incarnation lifecycle."""

from __future__ import annotations

from typing import Protocol

from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
)


class IncarnationStopPort(Protocol):
    """OS capability that pins every signal to one process lifetime."""

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability: ...

    def stop_expected(self, command: StopEngineCommand) -> StopEngineOutcome: ...


class RepositoryEngineLifecycle(Protocol):
    """Plain exact-engine lifecycle, independent of validated-work records."""

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability: ...

    def stop_engine(self, command: StopEngineCommand) -> StopEngineOutcome: ...
