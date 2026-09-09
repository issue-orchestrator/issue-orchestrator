"""Plain lifecycle owner and exact command contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.domain.repository_engine_lifecycle import (
    ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS,
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
    StopEngineStatus,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.execution.repository_engine_lifecycle import (
    SupervisorRepositoryEngineLifecycle,
)


PROCESS = ProcessIdentity("local", 123, "linux-proc-v1:boot-a:12345", "engine-a")
ENGINE = EngineIdentity("/repo", "engine-a", "local", "engine-a", PROCESS)


class StopCapability:
    availability: object = EngineStopAvailability.AVAILABLE
    outcome: object = StopEngineOutcome(StopEngineStatus.STOPPED, ENGINE, "stopped")

    def __init__(self) -> None:
        self.availability_calls: list[EngineIdentity] = []
        self.stop_calls: list[StopEngineCommand] = []

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability:
        self.availability_calls.append(engine)
        return self.availability  # type: ignore[return-value]

    def stop_expected(self, command: StopEngineCommand) -> StopEngineOutcome:
        self.stop_calls.append(command)
        return self.outcome  # type: ignore[return-value]


def test_command_states_explicit_graceful_then_force_policy() -> None:
    command = StopEngineCommand(ENGINE, "operator", "release wedged owner")

    assert command.graceful_timeout_seconds == ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS
    assert command.force_on_timeout is True


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_command_rejects_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        StopEngineCommand(ENGINE, "actor", "reason", timeout)


def test_identity_command_and_outcome_reject_untyped_or_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="disagree"):
        EngineIdentity("/repo", None, "local", "default", PROCESS)
    with pytest.raises(ValueError, match="absolute"):
        replace(ENGINE, repo_root="relative")
    with pytest.raises(ValueError, match="path component"):
        bad_process = replace(PROCESS, instance_id="../escape")
        replace(ENGINE, instance_id="../escape", process=bad_process)
    with pytest.raises(ValueError, match="actor"):
        StopEngineCommand(ENGINE, "", "reason")
    with pytest.raises(ValueError, match="reason"):
        StopEngineCommand(ENGINE, "actor", "")
    with pytest.raises(ValueError, match="boolean"):
        StopEngineCommand(ENGINE, "actor", "reason", force_on_timeout=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="status"):
        StopEngineOutcome("stopped", ENGINE, "done")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message"):
        StopEngineOutcome(StopEngineStatus.STOPPED, ENGINE, "")


def test_remote_engine_is_refused_without_touching_local_capability() -> None:
    capability = StopCapability()
    lifecycle = SupervisorRepositoryEngineLifecycle(capability, local_host="local")
    remote_process = replace(PROCESS, host="remote")
    remote = replace(ENGINE, host="remote", process=remote_process)
    command = StopEngineCommand(remote, "operator", "reason")

    assert lifecycle.stop_availability(remote) is EngineStopAvailability.REMOTE_HOST
    outcome = lifecycle.stop_engine(command)

    assert outcome.status is StopEngineStatus.REMOTE_HOST
    assert outcome.engine == remote
    assert capability.availability_calls == []
    assert capability.stop_calls == []


def test_local_command_is_forwarded_unchanged() -> None:
    capability = StopCapability()
    lifecycle = SupervisorRepositoryEngineLifecycle(capability, local_host="local")
    command = StopEngineCommand(ENGINE, "operator", "reason", 7.5, False)

    assert lifecycle.stop_engine(command) == capability.outcome
    assert capability.stop_calls == [command]


@pytest.mark.parametrize(
    "availability",
    [
        EngineStopAvailability.EXACT_TARGET_UNAVAILABLE,
        EngineStopAvailability.REMOTE_HOST,
    ],
)
def test_capability_loss_after_render_fails_without_dispatch(
    availability: EngineStopAvailability,
) -> None:
    capability = StopCapability()
    capability.availability = availability
    lifecycle = SupervisorRepositoryEngineLifecycle(capability, local_host="local")

    outcome = lifecycle.stop_engine(StopEngineCommand(ENGINE, "operator", "reason"))

    assert outcome.status is StopEngineStatus.FAILED
    assert availability.value in outcome.message
    assert capability.stop_calls == []


def test_lifecycle_rejects_untyped_or_misdirected_adapter_results() -> None:
    capability = StopCapability()
    lifecycle = SupervisorRepositoryEngineLifecycle(capability, local_host="local")
    capability.availability = "available"
    with pytest.raises(TypeError, match="availability"):
        lifecycle.stop_availability(ENGINE)

    capability.availability = EngineStopAvailability.AVAILABLE
    capability.outcome = "stopped"
    with pytest.raises(TypeError, match="outcome"):
        lifecycle.stop_engine(StopEngineCommand(ENGINE, "operator", "reason"))

    other = replace(ENGINE, repo_root="/other")
    capability.outcome = StopEngineOutcome(StopEngineStatus.STOPPED, other, "stopped")
    with pytest.raises(ValueError, match="different engine"):
        lifecycle.stop_engine(StopEngineCommand(ENGINE, "operator", "reason"))
