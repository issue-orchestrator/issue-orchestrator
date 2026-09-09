"""Guard an exact Repository Engine stop with durable record ownership."""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from ..domain.read_only_sqlite import ReadOnlySqliteAccessError
from ..domain.repository_engine_lifecycle import (
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
    StopEngineStatus,
)
from ..domain.validated_work import require_text
from ..domain.validated_work_discovery import ClaimOwnerFact, ValidatedWorkSnapshot
from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopOwnerStatus,
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)
from ..ports.repository_engine_lifecycle import RepositoryEngineLifecycle
from ..ports.validated_work_record_reader import ValidatedWorkRecordReader
from ..ports.validated_work_stop_reservations import ValidatedWorkStopReservations


class GuardedValidatedWorkOwnerStop:
    """Own the read, reserve, exact-stop and release operation lifetime."""

    def __init__(
        self,
        *,
        repo_root: str,
        reader: ValidatedWorkRecordReader,
        reservations: ValidatedWorkStopReservations,
        lifecycle: RepositoryEngineLifecycle,
    ) -> None:
        require_text(repo_root, "repository root")
        root = Path(repo_root)
        if not root.is_absolute():
            raise ValueError("guarded owner stop requires an absolute repository root")
        self._repo_root = str(root.resolve())
        self._reader = reader
        self._reservations = reservations
        self._lifecycle = lifecycle

    def stop_owning_engine(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopOwnerOutcome:
        if type(command) is not StopValidatedWorkOwnerCommand:
            raise TypeError("guarded owner stop requires a typed command")
        if str(Path(command.expected_engine.repo_root).resolve()) != self._repo_root:
            return StopOwnerOutcome(
                StopOwnerStatus.REPO_MISMATCH,
                None,
                "The selected engine belongs to a different repository",
            )

        snapshot = self._initial_snapshot(command)
        if isinstance(snapshot, StopOwnerOutcome):
            return snapshot
        owner = snapshot.owner
        if owner is None:
            return StopOwnerOutcome(
                StopOwnerStatus.NOT_OWNED,
                None,
                "The validated-work record no longer has an owning engine",
            )
        mismatch = self._owner_mismatch(command, owner)
        if mismatch is not None:
            return mismatch
        if owner.stop_availability is EngineStopAvailability.REMOTE_HOST:
            return StopOwnerOutcome(
                StopOwnerStatus.REMOTE_HOST,
                owner,
                "The owning engine runs on a different host",
            )

        admitted = self._reservations.reserve_owner_stop(command)
        if isinstance(admitted, StopReservationRefusal):
            return StopOwnerOutcome(
                admitted.status, admitted.observed_owner, admitted.message
            )
        if type(admitted) is not StopReservation:
            raise TypeError("reservation port returned an untyped result")
        try:
            self._require_reserved_owner(admitted, owner, command.record_id)
            return self._stop_reserved_owner(command, admitted, owner)
        finally:
            self._reservations.release_owner_stop(admitted)

    def _initial_snapshot(
        self, command: StopValidatedWorkOwnerCommand
    ) -> ValidatedWorkSnapshot | StopOwnerOutcome:
        try:
            snapshot = self._reader.snapshot_record(self._repo_root, command.record_id)
        except ReadOnlySqliteAccessError as error:
            return StopOwnerOutcome(
                StopOwnerStatus.RECORD_UNAVAILABLE,
                None,
                f"Validated-work ownership is unavailable: {error}",
            )
        if snapshot is None:
            return StopOwnerOutcome(
                StopOwnerStatus.NO_SUCH_RECORD,
                None,
                "The validated-work record no longer exists",
            )
        if type(snapshot) is not ValidatedWorkSnapshot:
            raise TypeError("record reader returned an untyped snapshot")
        if snapshot.record_id != command.record_id:
            raise ValueError("record reader returned a different validated-work record")
        return snapshot

    @staticmethod
    def _owner_mismatch(
        command: StopValidatedWorkOwnerCommand, owner: ClaimOwnerFact
    ) -> StopOwnerOutcome | None:
        if (
            owner.engine == command.expected_engine
            and owner.owner_fence == command.expected_owner_fence
        ):
            return None
        return StopOwnerOutcome(
            StopOwnerStatus.OWNER_CHANGED,
            owner,
            "The validated-work owner changed; refresh before stopping it",
        )

    @staticmethod
    def _require_reserved_owner(
        reservation: StopReservation, owner: ClaimOwnerFact, record_id: str
    ) -> None:
        if (
            reservation.record_id != record_id
            or reservation.engine != owner.engine
            or reservation.owner_fence != owner.owner_fence
        ):
            raise ValueError("reservation does not match the observed record owner")

    def _stop_reserved_owner(
        self,
        command: StopValidatedWorkOwnerCommand,
        reservation: StopReservation,
        owner: ClaimOwnerFact,
    ) -> StopOwnerOutcome:
        try:
            result = self._lifecycle.stop_engine(
                StopEngineCommand(
                    reservation.engine,
                    command.actor,
                    command.reason,
                )
            )
        except Exception as error:
            return StopOwnerOutcome(
                StopOwnerStatus.STOP_FAILED,
                owner,
                f"Exact engine stop failed: {error}",
            )
        if type(result) is not StopEngineOutcome:
            raise TypeError("engine lifecycle returned an untyped result")
        if result.engine != reservation.engine:
            raise ValueError("engine lifecycle returned a different engine identity")
        match result.status:
            case StopEngineStatus.STOPPED:
                return StopOwnerOutcome(StopOwnerStatus.STOPPED, owner, result.message)
            case StopEngineStatus.FAILED:
                return StopOwnerOutcome(
                    StopOwnerStatus.STOP_FAILED, owner, result.message
                )
            case StopEngineStatus.REMOTE_HOST:
                return StopOwnerOutcome(
                    StopOwnerStatus.REMOTE_HOST, owner, result.message
                )
            case StopEngineStatus.TARGET_CHANGED:
                return self._changed_target(command, owner)
            case _:
                assert_never(result.status)

    def _changed_target(
        self, command: StopValidatedWorkOwnerCommand, matched_owner: ClaimOwnerFact
    ) -> StopOwnerOutcome:
        try:
            current = self._reader.snapshot_record(self._repo_root, command.record_id)
        except ReadOnlySqliteAccessError as error:
            return StopOwnerOutcome(
                StopOwnerStatus.STOP_FAILED,
                matched_owner,
                f"The engine target changed and ownership could not be re-read: {error}",
            )
        if current is not None and type(current) is not ValidatedWorkSnapshot:
            raise TypeError("record reader returned an untyped snapshot")
        if current is not None and current.record_id != command.record_id:
            raise ValueError("record reader returned a different validated-work record")
        return StopOwnerOutcome(
            StopOwnerStatus.OWNER_CHANGED,
            None if current is None else current.owner,
            "The engine incarnation changed before the stop effect",
        )
