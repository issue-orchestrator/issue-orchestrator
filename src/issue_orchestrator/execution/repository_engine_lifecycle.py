"""Policy owner for exact Repository Engine lifecycle operations."""

from __future__ import annotations

from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
    StopEngineStatus,
)
from ..domain.validated_work import require_text
from ..ports.repository_engine_lifecycle import IncarnationStopPort


class SupervisorRepositoryEngineLifecycle:
    """Refuse remote targets and delegate local lifetime-pinned effects."""

    def __init__(
        self, incarnation_stop: IncarnationStopPort, *, local_host: str
    ) -> None:
        if incarnation_stop is None:
            raise ValueError("engine lifecycle requires an incarnation stop capability")
        require_text(local_host, "local host")
        self._incarnation_stop = incarnation_stop
        self._local_host = local_host

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability:
        self._require_engine(engine)
        if engine.host != self._local_host:
            return EngineStopAvailability.REMOTE_HOST
        result = self._incarnation_stop.stop_availability(engine)
        if type(result) is not EngineStopAvailability:
            raise TypeError("incarnation stop returned untyped availability")
        return result

    def stop_engine(self, command: StopEngineCommand) -> StopEngineOutcome:
        if type(command) is not StopEngineCommand:
            raise TypeError("engine lifecycle requires a typed stop command")
        engine = command.engine
        if engine.host != self._local_host:
            return StopEngineOutcome(
                StopEngineStatus.REMOTE_HOST,
                engine,
                f"Engine {engine.label} runs on remote host {engine.host}",
            )
        availability = self.stop_availability(engine)
        if availability is not EngineStopAvailability.AVAILABLE:
            return StopEngineOutcome(
                StopEngineStatus.FAILED,
                engine,
                (
                    f"Exact process stop is unavailable for engine {engine.label}: "
                    f"{availability.value}"
                ),
            )
        result = self._incarnation_stop.stop_expected(command)
        if type(result) is not StopEngineOutcome:
            raise TypeError("incarnation stop returned an untyped outcome")
        if result.engine != engine:
            raise ValueError("incarnation stop returned a different engine identity")
        return result

    @staticmethod
    def _require_engine(engine: EngineIdentity) -> None:
        if type(engine) is not EngineIdentity:
            raise TypeError("engine lifecycle requires a typed engine identity")
