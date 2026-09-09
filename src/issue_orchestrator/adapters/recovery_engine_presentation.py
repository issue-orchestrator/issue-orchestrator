"""Supervisor-backed presentation of retained-work owner incarnations."""

from __future__ import annotations

from collections.abc import Callable

from ..domain.control_center_recovery import (
    EnginePresentationFact,
    RecoveryEnginePresentation,
)
from ..domain.repository_engine_lifecycle import EngineIdentity
from ..domain.validated_work import require_text
from ..infra.process_incarnation import (
    is_linux_process_incarnation,
    linux_process_incarnation,
)
from ..ports.repository_engine_supervisor import SupervisorOps

ProcessIncarnationReader = Callable[[int], str]


class SupervisorRecoveryEnginePresentation:
    """Compare supervisor observations with the exact durable claim owner."""

    def __init__(
        self,
        supervisor: SupervisorOps,
        *,
        local_host: str,
        process_incarnation: ProcessIncarnationReader = linux_process_incarnation,
    ) -> None:
        if supervisor is None:
            raise ValueError("engine presentation requires a supervisor")
        require_text(local_host, "local host")
        self._supervisor = supervisor
        self._local_host = local_host
        self._process_incarnation = process_incarnation

    def presentation_for(self, engine: EngineIdentity) -> EnginePresentationFact:
        if type(engine) is not EngineIdentity:
            raise TypeError("engine presentation requires a typed identity")
        if engine.host != self._local_host:
            return self._fact(
                RecoveryEnginePresentation.UNKNOWN,
                f"Engine {engine.label} is owned by remote host {engine.host}",
            )
        try:
            observed = self._supervisor.status_all_instances(engine.repo_root)
        except Exception as error:
            return self._fact(
                RecoveryEnginePresentation.UNKNOWN,
                f"Could not read supervisor status for engine {engine.label}: {error}",
            )
        running_instance = tuple(
            status
            for status in observed.instances
            if status.instance_id == engine.instance_id and status.state == "running"
        )
        if not running_instance:
            return self._fact(
                RecoveryEnginePresentation.MISSING,
                f"Engine {engine.label} is no longer advertised",
            )
        if not any(status.pid == engine.process.pid for status in running_instance):
            return self._fact(
                RecoveryEnginePresentation.REPLACED,
                f"Engine {engine.label} now advertises a different process incarnation",
            )
        if not is_linux_process_incarnation(engine.process.started_at):
            return self._fact(
                RecoveryEnginePresentation.UNKNOWN,
                f"Exact process identity is unavailable for engine {engine.label}",
            )
        try:
            incarnation = self._process_incarnation(engine.process.pid)
        except (OSError, ValueError) as error:
            return self._fact(
                RecoveryEnginePresentation.UNKNOWN,
                f"Could not read exact process identity for engine {engine.label}: {error}",
            )
        if incarnation == engine.process.started_at:
            return self._fact(
                RecoveryEnginePresentation.OBSERVED,
                f"Engine {engine.label} advertises the retained-work owner incarnation",
            )
        return self._fact(
            RecoveryEnginePresentation.REPLACED,
            f"Engine {engine.label} now advertises a different process incarnation",
        )

    @staticmethod
    def _fact(
        presentation: RecoveryEnginePresentation,
        message: str,
    ) -> EnginePresentationFact:
        return EnginePresentationFact(presentation, message)


__all__ = ["SupervisorRecoveryEnginePresentation"]
