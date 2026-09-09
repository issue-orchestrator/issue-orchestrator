"""Control Center composition for cold retained-work recovery queries."""

import socket
from pathlib import Path

from ..adapters.configured_repository_registry import (
    RegisteredConfiguredRepositoryRegistry,
)
from ..adapters.recovery_engine_presentation import (
    SupervisorRecoveryEnginePresentation,
)
from ..adapters.repository_engine_incarnation import LinuxPidfdIncarnationStop
from ..control.control_center_recovery_queries import ControlCenterRecoveryQueries
from ..control.control_center_recovery_stops import ControlCenterRecoveryStops
from ..control.validated_work_owner_stop import GuardedValidatedWorkOwnerStop
from ..domain.control_center_recovery import ConfiguredRepository
from ..execution.repository_engine_lifecycle import SupervisorRepositoryEngineLifecycle
from ..infra.validated_work_record_reader import SqliteValidatedWorkRecordReader
from ..infra.validated_work_stop_reservations import (
    SqliteValidatedWorkStopReservations,
)
from ..ports.repository_engine_supervisor import SupervisorOps

CONTROL_CENTER_RECOVERY_READ_TIMEOUT_SECONDS = 5.0


def build_control_center_recovery_queries(
    supervisor: SupervisorOps,
) -> ControlCenterRecoveryQueries:
    """Wire cold discovery without a target Repository Engine dependency."""
    local_host = socket.gethostname()
    lifecycle = SupervisorRepositoryEngineLifecycle(
        LinuxPidfdIncarnationStop(),
        local_host=local_host,
    )
    return ControlCenterRecoveryQueries(
        repositories=RegisteredConfiguredRepositoryRegistry(),
        records=SqliteValidatedWorkRecordReader(
            timeout=CONTROL_CENTER_RECOVERY_READ_TIMEOUT_SECONDS,
            stop_availability=lifecycle.stop_availability,
        ),
        engine_presentations=SupervisorRecoveryEnginePresentation(
            supervisor,
            local_host=local_host,
        ),
    )


def build_control_center_recovery_stops(
    supervisor: SupervisorOps,
) -> ControlCenterRecoveryStops:
    """Wire guarded exact-owner stops behind configured repository scope."""
    local_host = socket.gethostname()
    lifecycle = SupervisorRepositoryEngineLifecycle(
        LinuxPidfdIncarnationStop(),
        local_host=local_host,
    )

    def stop_owner(repository: ConfiguredRepository) -> GuardedValidatedWorkOwnerStop:
        repo_root = Path(repository.repo_root)
        reader = SqliteValidatedWorkRecordReader(
            timeout=CONTROL_CENTER_RECOVERY_READ_TIMEOUT_SECONDS,
            stop_availability=lifecycle.stop_availability,
        )
        reservations = SqliteValidatedWorkStopReservations(
            repo_root=repo_root,
            repo_slug=repository.repo_slug,
            local_host=local_host,
            stop_availability=lifecycle.stop_availability,
            timeout=CONTROL_CENTER_RECOVERY_READ_TIMEOUT_SECONDS,
        )
        return GuardedValidatedWorkOwnerStop(
            repo_root=repository.repo_root,
            reader=reader,
            reservations=reservations,
            lifecycle=lifecycle,
        )

    return ControlCenterRecoveryStops(
        repositories=RegisteredConfiguredRepositoryRegistry(),
        stop_owner=stop_owner,
    )


__all__ = [
    "build_control_center_recovery_queries",
    "build_control_center_recovery_stops",
]
