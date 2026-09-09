"""Control Center composition for cold retained-work recovery queries."""

import socket

from ..adapters.configured_repository_registry import (
    RegisteredConfiguredRepositoryRegistry,
)
from ..adapters.recovery_engine_presentation import (
    SupervisorRecoveryEnginePresentation,
)
from ..adapters.repository_engine_incarnation import LinuxPidfdIncarnationStop
from ..control.control_center_recovery_queries import ControlCenterRecoveryQueries
from ..execution.repository_engine_lifecycle import SupervisorRepositoryEngineLifecycle
from ..infra.validated_work_record_reader import SqliteValidatedWorkRecordReader
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


__all__ = ["build_control_center_recovery_queries"]
