"""Control Center query owner for cold retained-work discovery."""

from __future__ import annotations

from typing import assert_never

from ..domain.control_center_recovery import (
    ConfiguredRepository,
    ControlCenterRecoveryRows,
    EnginePresentationFact,
    GuardedRecoveryStopAction,
    OwnedRecoveryRecord,
    RecoveryEngineGroup,
    RecoveryRecordFact,
    RecoveryRowsStatus,
    UnownedRecoveryRecord,
)
from ..domain.repository_engine_lifecycle import EngineIdentity, EngineStopAvailability
from ..domain.validated_work import require_text
from ..domain.validated_work_discovery import (
    ValidatedWorkDiscovery,
    ValidatedWorkSnapshot,
    WorkDiscoveryStatus,
)
from ..ports.configured_repository_registry import ConfiguredRepositoryRegistry
from ..ports.recovery_engine_presentation import RecoveryEnginePresentationReader
from ..ports.validated_work_record_reader import ValidatedWorkRecordReader


class UnknownConfiguredRepositoryError(KeyError):
    """The public repository key is absent from the configured registry."""


class ControlCenterRecoveryQueries:
    """Map one configured repository's cold SQLite facts into strict UI rows."""

    def __init__(
        self,
        *,
        repositories: ConfiguredRepositoryRegistry,
        records: ValidatedWorkRecordReader,
        engine_presentations: RecoveryEnginePresentationReader,
    ) -> None:
        self._repositories = repositories
        self._records = records
        self._engine_presentations = engine_presentations

    def repository(self, repo_key: str) -> ControlCenterRecoveryRows:
        """Discover by registry key, never by a caller-supplied filesystem root."""
        require_text(repo_key, "configured repository key")
        repository = self._repositories.resolve(repo_key)
        if repository is None:
            raise UnknownConfiguredRepositoryError(repo_key)
        if type(repository) is not ConfiguredRepository:
            raise TypeError("repository registry returned an untyped repository")
        if repository.repo_key != repo_key:
            raise ValueError("repository registry returned a different public key")
        discovery = self._records.discover_repository(repository.repo_root)
        if type(discovery) is not ValidatedWorkDiscovery:
            raise TypeError("validated-work reader returned an untyped discovery")
        return self._project(repository, discovery)

    def _project(
        self,
        repository: ConfiguredRepository,
        discovery: ValidatedWorkDiscovery,
    ) -> ControlCenterRecoveryRows:
        match discovery.status:
            case WorkDiscoveryStatus.AVAILABLE:
                return self._available(repository, discovery)
            case WorkDiscoveryStatus.DATABASE_ABSENT:
                status = RecoveryRowsStatus.DATABASE_ABSENT
            case WorkDiscoveryStatus.UNREADABLE:
                status = RecoveryRowsStatus.UNREADABLE
            case WorkDiscoveryStatus.UNSUPPORTED_SCHEMA:
                status = RecoveryRowsStatus.UNSUPPORTED_SCHEMA
            case _:
                assert_never(discovery.status)
        return ControlCenterRecoveryRows(
            repository.repo_key,
            status,
            (),
            (),
            discovery.message,
        )

    def _available(
        self,
        repository: ConfiguredRepository,
        discovery: ValidatedWorkDiscovery,
    ) -> ControlCenterRecoveryRows:
        if not discovery.records:
            return ControlCenterRecoveryRows(
                repository.repo_key,
                RecoveryRowsStatus.EMPTY,
                (),
                (),
                "No preserved validated work",
            )
        groups: dict[EngineIdentity, list[OwnedRecoveryRecord]] = {}
        unowned: list[UnownedRecoveryRecord] = []
        for snapshot in sorted(discovery.records, key=_snapshot_order):
            self._validate_repository(repository, snapshot)
            work = _work_fact(snapshot)
            if snapshot.owner is None:
                unowned.append(UnownedRecoveryRecord("unowned", work))
                continue
            owner = snapshot.owner
            action = (
                GuardedRecoveryStopAction(
                    snapshot.record_id,
                    owner.engine,
                    owner.owner_fence,
                )
                if owner.stop_availability is EngineStopAvailability.AVAILABLE
                else None
            )
            groups.setdefault(owner.engine, []).append(
                OwnedRecoveryRecord("owned", work, owner, action)
            )
        engine_groups = tuple(
            self._engine_group(engine, tuple(rows))
            for engine, rows in sorted(
                groups.items(), key=lambda item: _engine_order(item[0])
            )
        )
        return ControlCenterRecoveryRows(
            repository.repo_key,
            RecoveryRowsStatus.AVAILABLE,
            engine_groups,
            tuple(unowned),
            discovery.message,
        )

    def _engine_group(
        self, engine: EngineIdentity, rows: tuple[OwnedRecoveryRecord, ...]
    ) -> RecoveryEngineGroup:
        presentation = self._engine_presentations.presentation_for(engine)
        if type(presentation) is not EnginePresentationFact:
            raise TypeError("engine presentation reader returned an untyped fact")
        return RecoveryEngineGroup(
            engine,
            presentation.presentation,
            presentation.message,
            rows,
        )

    @staticmethod
    def _validate_repository(
        repository: ConfiguredRepository,
        snapshot: ValidatedWorkSnapshot,
    ) -> None:
        if snapshot.disposition.key.repo_slug != repository.repo_slug:
            raise ValueError(
                "discovered record does not belong to configured repository"
            )
        if (
            snapshot.owner is not None
            and snapshot.owner.engine.repo_root != repository.repo_root
        ):
            raise ValueError(
                "record owner does not belong to configured repository root"
            )


def _work_fact(snapshot: ValidatedWorkSnapshot) -> RecoveryRecordFact:
    disposition = snapshot.disposition
    return RecoveryRecordFact(
        authority=snapshot.authority,
        state=disposition.state,
        failure=disposition.failure,
        reason=disposition.reason,
        escrow_retained=snapshot.escrow_retained,
    )


def _snapshot_order(snapshot: ValidatedWorkSnapshot) -> tuple[int, str, str]:
    return (
        snapshot.disposition.key.issue_number,
        snapshot.branch_name,
        snapshot.record_id,
    )


def _engine_order(engine: EngineIdentity) -> tuple[str, str, str, int, str, str]:
    return (
        engine.host,
        engine.instance_id or "",
        engine.process.host,
        engine.process.pid,
        engine.process.started_at,
        engine.label,
    )
