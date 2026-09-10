"""Guarded coordination from durable owner fact to exact lifecycle effect."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import pytest

from issue_orchestrator.control.validated_work_owner_stop import (
    GuardedValidatedWorkOwnerStop,
)
from issue_orchestrator.domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
    StopEngineStatus,
)
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase,
    LineageRole,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_commands import (
    AbandonStatus,
    ValidatedWorkDisposition,
)
from issue_orchestrator.domain.validated_work_discovery import (
    ClaimOwnerFact,
    ValidatedWorkSnapshot,
)
from issue_orchestrator.domain.validated_work_owner_stop import (
    StopOwnerStatus,
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)


PROCESS = ProcessIdentity("local", 42, "linux-proc-v1:boot:17", "engine-a")
ENGINE = EngineIdentity("/repo", "engine-a", "local", "engine-a", PROCESS)
OWNER = ClaimOwnerFact(ENGINE, 3, EngineStopAvailability.AVAILABLE)
RECORD_ID = ValidatedWorkKey("owner/repo", 7, "feature", "1" * 40).record_id
COMMAND = StopValidatedWorkOwnerCommand(RECORD_ID, ENGINE, 3, "operator", "wedged")
RESERVATION = StopReservation("reservation", RECORD_ID, ENGINE, 3)


def _snapshot(
    owner: ClaimOwnerFact | None = OWNER, *, issue_number: int = 7
) -> ValidatedWorkSnapshot:
    key = ValidatedWorkKey("owner/repo", issue_number, "feature", "1" * 40)
    disposition = ValidatedWorkDisposition(
        key.record_id,
        key,
        "evidence-1",
        ValidatedWorkState.PARKED,
        LineageRole.HEAD,
        "retained",
    )
    return ValidatedWorkSnapshot(
        disposition=disposition,
        record_id=disposition.record_id,
        validated_head_sha=key.validated_head_sha,
        worktree_head_sha=key.validated_head_sha,
        branch_name=key.branch_name,
        expected_remote_head_sha=None,
        superseded_evidence_ids=(),
        attached_evidence_ids=(),
        lineage_role=LineageRole.HEAD,
        escrow_retained=True,
        observation_revision=0,
        waits_on_record_id="",
        owner=owner,
        publish_attempts=0,
        finalization_phase=FinalizationPhase.NOT_STARTED,
        updated_at="2026-01-01T00:00:00+00:00",
        can_recover=owner is None,
        can_abandon=owner is None,
        abandon_unavailable=None if owner is None else AbandonStatus.REFUSED_STATE,
    )


@dataclass
class Reader:
    results: list[ValidatedWorkSnapshot | None | Exception]
    calls: list[tuple[str, str]] = field(default_factory=list)

    def snapshot_record(self, repo_root: str, record_id: str):
        self.calls.append((repo_root, record_id))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def discover_repository(self, repo_root: str):
        raise AssertionError("coordinator must use exact record lookup")


@dataclass
class Reservations:
    result: StopReservation | StopReservationRefusal = RESERVATION
    reserved: list[StopValidatedWorkOwnerCommand] = field(default_factory=list)
    released: list[StopReservation] = field(default_factory=list)

    def reserve_owner_stop(self, command: StopValidatedWorkOwnerCommand):
        self.reserved.append(command)
        return self.result

    def release_owner_stop(self, reservation: StopReservation) -> bool:
        self.released.append(reservation)
        return True


@dataclass
class Lifecycle:
    result: StopEngineOutcome | Exception = field(
        default_factory=lambda: StopEngineOutcome(
            StopEngineStatus.STOPPED, ENGINE, "engine stopped"
        )
    )
    calls: list[StopEngineCommand] = field(default_factory=list)

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability:
        return EngineStopAvailability.AVAILABLE

    def stop_engine(self, command: StopEngineCommand):
        self.calls.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _coordinator(
    reader: Reader,
    reservations: Reservations | None = None,
    lifecycle: Lifecycle | None = None,
) -> tuple[GuardedValidatedWorkOwnerStop, Reservations, Lifecycle]:
    reserved = reservations or Reservations()
    engines = lifecycle or Lifecycle()
    return (
        GuardedValidatedWorkOwnerStop(
            repo_root="/repo",
            reader=reader,
            reservations=reserved,
            lifecycle=engines,
        ),
        reserved,
        engines,
    )


def test_repository_path_mismatch_stops_before_read_or_effect() -> None:
    reader = Reader([_snapshot()])
    coordinator, reservations, lifecycle = _coordinator(reader)
    other = replace(ENGINE, repo_root="/other")

    result = coordinator.stop_owning_engine(replace(COMMAND, expected_engine=other))

    assert result.status is StopOwnerStatus.REPO_MISMATCH
    assert result.observed_owner is None
    assert reader.calls == []
    assert reservations.reserved == []
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    ("observed", "status", "owner"),
    [
        (None, StopOwnerStatus.NO_SUCH_RECORD, None),
        (_snapshot(None), StopOwnerStatus.NOT_OWNED, None),
        (
            _snapshot(replace(OWNER, owner_fence=4)),
            StopOwnerStatus.OWNER_CHANGED,
            replace(OWNER, owner_fence=4),
        ),
        (
            _snapshot(
                replace(OWNER, stop_availability=EngineStopAvailability.REMOTE_HOST)
            ),
            StopOwnerStatus.REMOTE_HOST,
            replace(OWNER, stop_availability=EngineStopAvailability.REMOTE_HOST),
        ),
    ],
)
def test_pre_reservation_refusals_have_zero_write_or_lifecycle_effect(
    observed: ValidatedWorkSnapshot | None,
    status: StopOwnerStatus,
    owner: ClaimOwnerFact | None,
) -> None:
    coordinator, reservations, lifecycle = _coordinator(Reader([observed]))

    result = coordinator.stop_owning_engine(COMMAND)

    assert result.status is status
    assert result.observed_owner == owner
    assert reservations.reserved == []
    assert lifecycle.calls == []


def test_pre_reservation_read_failure_is_unavailable_with_zero_effect() -> None:
    failure = ReadOnlySqliteAccessError(
        ReadOnlySqliteFailure.TIMEOUT, "database remained locked"
    )
    coordinator, reservations, lifecycle = _coordinator(Reader([failure]))

    result = coordinator.stop_owning_engine(COMMAND)

    assert result.status is StopOwnerStatus.RECORD_UNAVAILABLE
    assert result.observed_owner is None
    assert reservations.reserved == []
    assert lifecycle.calls == []


def test_initial_reader_cannot_substitute_a_different_record() -> None:
    coordinator, reservations, lifecycle = _coordinator(
        Reader([_snapshot(issue_number=8)])
    )

    with pytest.raises(ValueError, match="different validated-work record"):
        coordinator.stop_owning_engine(COMMAND)

    assert reservations.reserved == []
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    "refusal",
    [
        StopReservationRefusal(
            StopOwnerStatus.NO_SUCH_RECORD, None, "record disappeared"
        ),
        StopReservationRefusal(StopOwnerStatus.NOT_OWNED, None, "owner left"),
        StopReservationRefusal(StopOwnerStatus.OWNER_CHANGED, OWNER, "owner changed"),
        StopReservationRefusal(
            StopOwnerStatus.STOP_IN_PROGRESS, OWNER, "already stopping"
        ),
        StopReservationRefusal(
            StopOwnerStatus.REPO_MISMATCH, OWNER, "repository changed"
        ),
        StopReservationRefusal(
            StopOwnerStatus.REMOTE_HOST, OWNER, "owner became remote"
        ),
    ],
)
def test_reservation_refusals_preserve_typed_provenance_and_do_not_stop(
    refusal: StopReservationRefusal,
) -> None:
    reservations = Reservations(refusal)
    coordinator, _, lifecycle = _coordinator(Reader([_snapshot()]), reservations)

    result = coordinator.stop_owning_engine(COMMAND)

    assert (result.status, result.observed_owner, result.message) == (
        refusal.status,
        refusal.observed_owner,
        refusal.message,
    )
    assert reservations.released == []
    assert lifecycle.calls == []


@pytest.mark.parametrize(
    ("engine_status", "owner_status"),
    [
        (StopEngineStatus.STOPPED, StopOwnerStatus.STOPPED),
        (StopEngineStatus.FAILED, StopOwnerStatus.STOP_FAILED),
        (StopEngineStatus.REMOTE_HOST, StopOwnerStatus.REMOTE_HOST),
    ],
)
def test_lifecycle_results_preserve_matched_owner_and_always_release(
    engine_status: StopEngineStatus, owner_status: StopOwnerStatus
) -> None:
    lifecycle = Lifecycle(StopEngineOutcome(engine_status, ENGINE, "result"))
    coordinator, reservations, _ = _coordinator(
        Reader([_snapshot()]), lifecycle=lifecycle
    )

    result = coordinator.stop_owning_engine(COMMAND)

    assert result.status is owner_status
    assert result.observed_owner == OWNER
    assert lifecycle.calls == [StopEngineCommand(ENGINE, "operator", "wedged")]
    assert reservations.released == [RESERVATION]


def test_lifecycle_exception_is_failed_and_releases_the_reservation() -> None:
    lifecycle = Lifecycle(RuntimeError("stop capability failed"))
    coordinator, reservations, _ = _coordinator(
        Reader([_snapshot()]), lifecycle=lifecycle
    )

    result = coordinator.stop_owning_engine(COMMAND)

    assert result.status is StopOwnerStatus.STOP_FAILED
    assert result.observed_owner == OWNER
    assert "stop capability failed" in result.message
    assert reservations.released == [RESERVATION]


@pytest.mark.parametrize(
    ("second", "expected_owner", "expected_status"),
    [
        (None, None, StopOwnerStatus.OWNER_CHANGED),
        (_snapshot(None), None, StopOwnerStatus.OWNER_CHANGED),
        (
            _snapshot(replace(OWNER, owner_fence=4)),
            replace(OWNER, owner_fence=4),
            StopOwnerStatus.OWNER_CHANGED,
        ),
        (
            ReadOnlySqliteAccessError(
                ReadOnlySqliteFailure.UNREADABLE, "re-read failed"
            ),
            OWNER,
            StopOwnerStatus.STOP_FAILED,
        ),
    ],
)
def test_changed_lifecycle_target_rechecks_without_a_second_stop(
    second: ValidatedWorkSnapshot | None | Exception,
    expected_owner: ClaimOwnerFact | None,
    expected_status: StopOwnerStatus,
) -> None:
    lifecycle = Lifecycle(
        StopEngineOutcome(StopEngineStatus.TARGET_CHANGED, ENGINE, "changed")
    )
    reader = Reader([_snapshot(), second])
    coordinator, reservations, _ = _coordinator(reader, lifecycle=lifecycle)

    result = coordinator.stop_owning_engine(COMMAND)

    assert result.status is expected_status
    assert result.observed_owner == expected_owner
    assert len(lifecycle.calls) == 1
    assert reservations.released == [RESERVATION]


def test_reservation_mismatch_is_an_invariant_failure_but_still_releases() -> None:
    mismatched = replace(RESERVATION, owner_fence=4)
    reservations = Reservations(mismatched)
    coordinator, _, lifecycle = _coordinator(Reader([_snapshot()]), reservations)

    with pytest.raises(ValueError, match="does not match"):
        coordinator.stop_owning_engine(COMMAND)

    assert lifecycle.calls == []
    assert reservations.released == [mismatched]


def test_reservation_cannot_substitute_a_different_record() -> None:
    mismatched = replace(RESERVATION, record_id="another-record")
    reservations = Reservations(mismatched)
    coordinator, _, lifecycle = _coordinator(Reader([_snapshot()]), reservations)

    with pytest.raises(ValueError, match="does not match"):
        coordinator.stop_owning_engine(COMMAND)

    assert lifecycle.calls == []
    assert reservations.released == [mismatched]


def test_lifecycle_cannot_substitute_a_different_engine_identity() -> None:
    replacement_process = replace(PROCESS, started_at="linux-proc-v1:boot:later")
    replacement = replace(ENGINE, process=replacement_process)
    lifecycle = Lifecycle(
        StopEngineOutcome(StopEngineStatus.STOPPED, replacement, "wrong engine")
    )
    coordinator, reservations, _ = _coordinator(
        Reader([_snapshot()]), lifecycle=lifecycle
    )

    with pytest.raises(ValueError, match="different engine"):
        coordinator.stop_owning_engine(COMMAND)

    assert reservations.released == [RESERVATION]


def test_target_change_reader_cannot_substitute_a_different_record() -> None:
    lifecycle = Lifecycle(
        StopEngineOutcome(StopEngineStatus.TARGET_CHANGED, ENGINE, "changed")
    )
    coordinator, reservations, _ = _coordinator(
        Reader([_snapshot(), _snapshot(issue_number=8)]), lifecycle=lifecycle
    )

    with pytest.raises(ValueError, match="different validated-work record"):
        coordinator.stop_owning_engine(COMMAND)

    assert len(lifecycle.calls) == 1
    assert reservations.released == [RESERVATION]
