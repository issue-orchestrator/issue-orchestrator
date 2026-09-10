"""Route-scope policy tests for Control Center exact-owner stops."""

from dataclasses import dataclass, field

import pytest

from issue_orchestrator.control.control_center_recovery_queries import (
    UnknownConfiguredRepositoryError,
)
from issue_orchestrator.control.control_center_recovery_stops import (
    ControlCenterRecoveryStops,
)
from issue_orchestrator.domain.control_center_recovery import ConfiguredRepository
from issue_orchestrator.domain.repository_engine_lifecycle import EngineIdentity
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopOwnerStatus,
    StopValidatedWorkOwnerCommand,
)

REPO_KEY = "repo-" + "a" * 64
REPOSITORY = ConfiguredRepository(REPO_KEY, "/repo", "owner/repo")
ENGINE = EngineIdentity(
    "/repo",
    "worker-a",
    "host-a",
    "worker-a",
    ProcessIdentity("host-a", 1234, "12345", "worker-a"),
)
COMMAND = StopValidatedWorkOwnerCommand(
    "record-a",
    ENGINE,
    7,
    "control-center.validated-work-stop",
    "Engine is wedged",
)


@dataclass
class Registry:
    repository: ConfiguredRepository | None = REPOSITORY

    def resolve(self, repo_key: str) -> ConfiguredRepository | None:
        return self.repository if repo_key == REPO_KEY else None


@dataclass
class Owner:
    result: StopOwnerOutcome
    commands: list[StopValidatedWorkOwnerCommand] = field(default_factory=list)

    def stop_owning_engine(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopOwnerOutcome:
        self.commands.append(command)
        return self.result


def owner_result(
    status: StopOwnerStatus = StopOwnerStatus.REPO_MISMATCH,
) -> StopOwnerOutcome:
    return StopOwnerOutcome(status, None, status.value)


def test_route_scope_delegates_only_the_matching_repository_and_instance() -> None:
    owner = Owner(owner_result())
    stops = ControlCenterRecoveryStops(
        repositories=Registry(),
        stop_owner=lambda repository: owner,
    )

    result = stops.stop_owning_engine(REPO_KEY, "worker-a", COMMAND)

    assert result == owner.result
    assert owner.commands == [COMMAND]


@pytest.mark.parametrize(
    ("repo_key", "instance_key", "command", "message"),
    [
        (
            REPO_KEY,
            "worker-b",
            COMMAND,
            "selected route does not match",
        ),
        (
            REPO_KEY,
            "worker-a",
            StopValidatedWorkOwnerCommand(
                "record-a",
                EngineIdentity(
                    "/other",
                    "worker-a",
                    "host-a",
                    "worker-a",
                    ProcessIdentity("host-a", 1234, "12345", "worker-a"),
                ),
                7,
                "control-center.validated-work-stop",
                "Engine is wedged",
            ),
            "different repository",
        ),
        (
            REPO_KEY,
            "default",
            StopValidatedWorkOwnerCommand(
                "record-a",
                EngineIdentity(
                    "/repo",
                    "default",
                    "host-a",
                    "default",
                    ProcessIdentity("host-a", 1234, "12345", "default"),
                ),
                7,
                "control-center.validated-work-stop",
                "Engine is wedged",
            ),
            "unroutable instance identity",
        ),
    ],
)
def test_route_scope_refuses_mismatches_before_constructing_an_owner(
    repo_key: str,
    instance_key: str,
    command: StopValidatedWorkOwnerCommand,
    message: str,
) -> None:
    constructed: list[ConfiguredRepository] = []

    def construct_owner(repository: ConfiguredRepository) -> Owner:
        constructed.append(repository)
        raise AssertionError("mismatched scope must not construct an owner")

    stops = ControlCenterRecoveryStops(
        repositories=Registry(),
        stop_owner=construct_owner,
    )

    result = stops.stop_owning_engine(repo_key, instance_key, command)

    assert result.status is StopOwnerStatus.REPO_MISMATCH
    assert message in result.message
    assert constructed == []


def test_route_scope_refuses_unknown_repository_without_constructing_an_owner() -> None:
    def construct_owner(repository: ConfiguredRepository) -> Owner:
        raise AssertionError(
            f"unknown repository must not construct an owner: {repository}"
        )

    stops = ControlCenterRecoveryStops(
        repositories=Registry(None),
        stop_owner=construct_owner,
    )

    with pytest.raises(UnknownConfiguredRepositoryError):
        stops.stop_owning_engine(REPO_KEY, "worker-a", COMMAND)
