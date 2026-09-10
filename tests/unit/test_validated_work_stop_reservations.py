"""Durable compare-and-set reservations around exact owner stops."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from issue_orchestrator.domain.read_only_sqlite import ReadOnlySqliteAccessError
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_owner_stop import (
    StopOwnerStatus,
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)
from issue_orchestrator.infra.validated_work_stop_reservations import (
    SqliteValidatedWorkStopReservations,
)

from .validated_work_support import AT, OTHER, OWNER, Liveness, Rig, capture, claim


def _database(repo: Path) -> Path:
    return repo / ".issue-orchestrator" / "state" / "validated_work.sqlite"


def _availability(engine: EngineIdentity) -> EngineStopAvailability:
    return (
        EngineStopAvailability.AVAILABLE
        if engine.host == OWNER.host
        else EngineStopAvailability.REMOTE_HOST
    )


def _adapter(
    repo: Path,
    *,
    local_host: str = OWNER.host,
    reservation_id=lambda: "reservation-id",
) -> SqliteValidatedWorkStopReservations:
    return SqliteValidatedWorkStopReservations(
        repo_root=repo,
        repo_slug="owner/repo",
        local_host=local_host,
        stop_availability=_availability,
        timeout=2,
        reservation_id=reservation_id,
        clock=lambda: datetime.fromisoformat(AT),
    )


def _owned(repo: Path):
    rig = Rig(_database(repo))
    store = rig.open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    token = claim(store, admission)
    engine = EngineIdentity(
        str(repo.resolve()),
        OWNER.instance_id,
        OWNER.host,
        OWNER.instance_id or "default",
        OWNER,
    )
    command = StopValidatedWorkOwnerCommand(
        token.record_id, engine, token.fence, "operator", "owner is wedged"
    )
    return rig, store, admission, token, command


def _reservation_columns(repo: Path) -> tuple[object, ...]:
    with sqlite3.connect(_database(repo)) as conn:
        return conn.execute(
            "SELECT stop_reserved_fence,stop_reserved_engine,"
            "stop_reservation_id,stop_reserved_at FROM validated_work_records"
        ).fetchone()


def test_reservation_blocks_live_relinquish_until_exact_release(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _, store, _, token, command = _owned(repo)
    adapter = _adapter(repo)

    result = adapter.reserve_owner_stop(command)

    assert result == StopReservation(
        "reservation-id", command.record_id, command.expected_engine, token.fence
    )
    assert not store.relinquish_claim(token)
    assert store.holds_claim(token)
    duplicate = adapter.reserve_owner_stop(command)
    assert isinstance(duplicate, StopReservationRefusal)
    assert duplicate.status is StopOwnerStatus.STOP_IN_PROGRESS
    assert duplicate.observed_owner is not None
    assert duplicate.observed_owner.engine == command.expected_engine
    assert adapter.release_owner_stop(result)
    assert not adapter.release_owner_stop(result)
    assert store.relinquish_claim(token)


def test_release_compares_the_full_reserved_incarnation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _, _, _, _, command = _owned(repo)
    adapter = _adapter(repo)
    reservation = adapter.reserve_owner_stop(command)
    assert isinstance(reservation, StopReservation)
    replacement_process = replace(
        reservation.engine.process, started_at="linux-proc-v1:boot:later"
    )
    replacement = replace(reservation.engine, process=replacement_process)

    assert not adapter.release_owner_stop(replace(reservation, engine=replacement))
    assert _reservation_columns(repo)[2] == reservation.reservation_id
    assert adapter.release_owner_stop(reservation)


def test_release_reasserts_repository_root_and_slug_authority(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _, _, _, _, command = _owned(repo)
    adapter = _adapter(repo)
    reservation = adapter.reserve_owner_stop(command)
    assert isinstance(reservation, StopReservation)
    wrong_root = replace(
        reservation,
        engine=replace(
            reservation.engine, repo_root=str((tmp_path / "other").resolve())
        ),
    )

    assert not adapter.release_owner_stop(wrong_root)
    with sqlite3.connect(_database(repo)) as conn:
        conn.execute("UPDATE validated_work_records SET repo_slug='other/repo'")
    assert not adapter.release_owner_stop(reservation)
    assert _reservation_columns(repo)[2] == reservation.reservation_id


@pytest.mark.parametrize(
    ("arrange", "status"),
    [
        ("missing", StopOwnerStatus.NO_SUCH_RECORD),
        ("unowned", StopOwnerStatus.NOT_OWNED),
        ("engine", StopOwnerStatus.OWNER_CHANGED),
        ("fence", StopOwnerStatus.OWNER_CHANGED),
        ("repo-root", StopOwnerStatus.REPO_MISMATCH),
        ("repo-slug", StopOwnerStatus.REPO_MISMATCH),
        ("remote", StopOwnerStatus.REMOTE_HOST),
    ],
)
def test_mismatch_refusals_make_no_reservation_write(
    tmp_path: Path, arrange: str, status: StopOwnerStatus
) -> None:
    repo = tmp_path / "repo"
    _, store, _, token, command = _owned(repo)
    adapter = _adapter(repo)
    if arrange == "missing":
        command = StopValidatedWorkOwnerCommand(
            "missing", command.expected_engine, token.fence, "operator", "wedged"
        )
    elif arrange == "unowned":
        assert store.relinquish_claim(token)
    elif arrange == "engine":
        different = ProcessIdentity(
            OWNER.host, OWNER.pid + 1, OWNER.started_at, OWNER.instance_id
        )
        command = StopValidatedWorkOwnerCommand(
            command.record_id,
            EngineIdentity(
                str(repo.resolve()), "engine-a", OWNER.host, "engine-a", different
            ),
            token.fence,
            "operator",
            "wedged",
        )
    elif arrange == "fence":
        command = StopValidatedWorkOwnerCommand(
            command.record_id,
            command.expected_engine,
            token.fence + 1,
            "operator",
            "wedged",
        )
    elif arrange == "repo-root":
        different = EngineIdentity(
            str((tmp_path / "other").resolve()),
            OWNER.instance_id,
            OWNER.host,
            "engine-a",
            OWNER,
        )
        command = StopValidatedWorkOwnerCommand(
            command.record_id, different, token.fence, "operator", "wedged"
        )
    elif arrange == "repo-slug":
        with sqlite3.connect(_database(repo)) as conn:
            conn.execute("UPDATE validated_work_records SET repo_slug='other/repo'")
    elif arrange == "remote":
        adapter = _adapter(repo, local_host="another-host")

    before = _reservation_columns(repo)
    result = adapter.reserve_owner_stop(command)

    assert isinstance(result, StopReservationRefusal)
    assert result.status is status
    assert _reservation_columns(repo) == before


def test_same_process_reacquire_advances_fence_and_refuses_old_render(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _, store, admission, old, old_command = _owned(repo)
    assert store.relinquish_claim(old)
    new = claim(store, admission)

    result = _adapter(repo).reserve_owner_stop(old_command)

    assert isinstance(result, StopReservationRefusal)
    assert result.status is StopOwnerStatus.OWNER_CHANGED
    assert result.observed_owner is not None
    assert result.observed_owner.owner_fence == new.fence
    assert _reservation_columns(repo) == (-1, "", "", "")


def test_owner_death_clears_reservation_and_stale_release_cannot_clear_successor(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rig, _, admission, old, old_command = _owned(repo)
    first = _adapter(repo, reservation_id=lambda: "first").reserve_owner_stop(
        old_command
    )
    assert isinstance(first, StopReservation)
    successor_store = rig.open(Liveness(OTHER, {OWNER}))
    successor = claim(successor_store, admission)
    successor_engine = EngineIdentity(
        str(repo.resolve()),
        OTHER.instance_id,
        OTHER.host,
        OTHER.instance_id or "default",
        OTHER,
    )
    successor_command = StopValidatedWorkOwnerCommand(
        successor.record_id,
        successor_engine,
        successor.fence,
        "operator",
        "wedged",
    )
    second = _adapter(
        repo, local_host=OTHER.host, reservation_id=lambda: "second"
    ).reserve_owner_stop(successor_command)
    assert isinstance(second, StopReservation)

    assert not _adapter(repo).release_owner_stop(first)
    assert _reservation_columns(repo)[2] == "second"


def test_concurrent_real_connections_admit_only_one_reservation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _, _, _, _, command = _owned(repo)
    start = Barrier(3)

    def reserve() -> StopReservation | StopReservationRefusal:
        start.wait()
        return _adapter(repo).reserve_owner_stop(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        start.wait()
        results = [future.result() for future in futures]

    assert sum(isinstance(item, StopReservation) for item in results) == 1
    refusals = [item for item in results if isinstance(item, StopReservationRefusal)]
    assert len(refusals) == 1
    assert refusals[0].status is StopOwnerStatus.STOP_IN_PROGRESS


def test_writable_adapter_never_creates_a_missing_database(tmp_path: Path) -> None:
    repo = tmp_path / "missing"
    process = OWNER
    engine = EngineIdentity(
        str(repo.resolve()), "engine-a", process.host, "engine-a", process
    )
    command = StopValidatedWorkOwnerCommand("record", engine, 1, "operator", "wedged")

    with pytest.raises(sqlite3.OperationalError):
        _adapter(repo).reserve_owner_stop(command)

    assert not repo.exists()


def test_writable_adapter_rejects_incompatible_schema_without_modifying_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    path = _database(repo)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated (value TEXT)")
        conn.execute("INSERT INTO unrelated VALUES ('preserved')")

    def dump() -> tuple[str, ...]:
        with sqlite3.connect(path) as conn:
            return tuple(conn.iterdump())

    before = dump()
    process = OWNER
    engine = EngineIdentity(
        str(repo.resolve()), "engine-a", process.host, "engine-a", process
    )
    command = StopValidatedWorkOwnerCommand("record", engine, 1, "operator", "wedged")

    with pytest.raises(ReadOnlySqliteAccessError, match="schema is unsupported"):
        _adapter(repo).reserve_owner_stop(command)

    assert dump() == before
