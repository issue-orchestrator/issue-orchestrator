"""Route-scoped policy owner for guarded retained-work engine stops."""

from __future__ import annotations

from collections.abc import Callable

from ..domain.control_center_recovery import (
    ConfiguredRepository,
    recovery_engine_instance_key,
)
from ..domain.validated_work import require_text
from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopOwnerStatus,
    StopValidatedWorkOwnerCommand,
)
from ..ports.configured_repository_registry import ConfiguredRepositoryRegistry
from ..ports.validated_work_owner_stop import ValidatedWorkOwnerStop
from .control_center_recovery_queries import UnknownConfiguredRepositoryError


StopOwnerFactory = Callable[[ConfiguredRepository], ValidatedWorkOwnerStop]


class ControlCenterRecoveryStops:
    """Bind public route scope before delegating to the repository stop owner."""

    def __init__(
        self,
        *,
        repositories: ConfiguredRepositoryRegistry,
        stop_owner: StopOwnerFactory,
    ) -> None:
        self._repositories = repositories
        self._stop_owner = stop_owner

    def stop_owning_engine(
        self,
        repo_key: str,
        instance_key: str,
        command: StopValidatedWorkOwnerCommand,
    ) -> StopOwnerOutcome:
        require_text(repo_key, "configured repository key")
        require_text(instance_key, "engine instance key")
        if type(command) is not StopValidatedWorkOwnerCommand:
            raise TypeError("Control Center stop requires a typed command")
        repository = self._repositories.resolve(repo_key)
        if repository is None:
            raise UnknownConfiguredRepositoryError(repo_key)
        if type(repository) is not ConfiguredRepository:
            raise TypeError("repository registry returned an untyped repository")
        if repository.repo_key != repo_key:
            raise ValueError("repository registry returned a different public key")
        if command.expected_engine.repo_root != repository.repo_root:
            return self._scope_mismatch(
                "The rendered engine belongs to a different repository"
            )
        try:
            expected_instance_key = recovery_engine_instance_key(
                command.expected_engine.instance_id
            )
        except ValueError:
            return self._scope_mismatch(
                "The rendered engine has an unroutable instance identity"
            )
        if instance_key != expected_instance_key:
            return self._scope_mismatch(
                "The selected route does not match the rendered engine instance"
            )
        owner = self._stop_owner(repository)
        if not hasattr(owner, "stop_owning_engine"):
            raise TypeError("stop-owner factory returned an invalid owner")
        result = owner.stop_owning_engine(command)
        if type(result) is not StopOwnerOutcome:
            raise TypeError("validated-work stop owner returned an untyped outcome")
        return result

    @staticmethod
    def _scope_mismatch(message: str) -> StopOwnerOutcome:
        return StopOwnerOutcome(StopOwnerStatus.REPO_MISMATCH, None, message)
