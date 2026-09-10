"""Atomic claim-stop reservations shared by store and Control Center adapters."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from math import ceil, isfinite
from pathlib import Path

from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from ..domain.validated_work import require_text
from ..domain.validated_work_claim import ProcessIdentity
from ..domain.validated_work_discovery import ClaimOwnerFact
from ..domain.validated_work_owner_stop import (
    StopOwnerStatus,
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)
from .repo_identity import normalize_repo_root, state_dir
from .sqlite_connection import apply_sqlite_pragmas, open_sqlite
from .validated_work_claims import owner_identity
from .validated_work_read_schema import require_supported_validated_work_schema


StopAvailability = Callable[[EngineIdentity], EngineStopAvailability]


def _fresh_reservation_id() -> str:
    return uuid.uuid4().hex


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StopReservationTransactions:
    """Own every predicate and write in the durable owner-stop interlock."""

    def __init__(
        self,
        *,
        repo_root: Path,
        repo_slug: str,
        local_host: str,
        stop_availability: StopAvailability,
        reservation_id: Callable[[], str] = _fresh_reservation_id,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repo_root = normalize_repo_root(repo_root)
        require_text(repo_slug, "repository slug")
        require_text(local_host, "local host")
        if not callable(stop_availability):
            raise ValueError("stop reservations require a stop-availability owner")
        if not callable(reservation_id) or not callable(clock):
            raise ValueError("stop reservations require id and clock capabilities")
        self._repo_slug = repo_slug
        self._local_host = local_host
        self._stop_availability = stop_availability
        self._reservation_id = reservation_id
        self._clock = clock

    def reserve(
        self, conn: sqlite3.Connection, command: StopValidatedWorkOwnerCommand
    ) -> StopReservation | StopReservationRefusal:
        if type(command) is not StopValidatedWorkOwnerCommand:
            raise TypeError("stop reservation requires a typed command")
        if normalize_repo_root(command.expected_engine.repo_root) != self._repo_root:
            return self._refusal(
                StopOwnerStatus.REPO_MISMATCH,
                None,
                "The selected engine belongs to a different repository",
            )
        row = conn.execute(
            "SELECT * FROM validated_work_records WHERE record_id=?",
            (command.record_id,),
        ).fetchone()
        if row is None:
            return self._refusal(
                StopOwnerStatus.NO_SUCH_RECORD,
                None,
                "The validated-work record no longer exists",
            )
        if row["repo_slug"] != self._repo_slug:
            return self._refusal(
                StopOwnerStatus.REPO_MISMATCH,
                None,
                "The validated-work record belongs to a different repository",
            )
        process = owner_identity(row)
        if process is None:
            return self._refusal(
                StopOwnerStatus.NOT_OWNED,
                None,
                "The validated-work record no longer has an owning engine",
            )
        observed = self._owner_fact(process, row["owner_fence"])
        if (
            command.expected_engine != observed.engine
            or command.expected_owner_fence != observed.owner_fence
        ):
            return self._refusal(
                StopOwnerStatus.OWNER_CHANGED,
                observed,
                "The validated-work owner changed; refresh before stopping it",
            )
        if (
            row["stop_reserved_fence"] == observed.owner_fence
            and row["stop_reservation_id"]
        ):
            return self._refusal(
                StopOwnerStatus.STOP_IN_PROGRESS,
                observed,
                "Another stop operation already reserved this owner",
            )
        if observed.engine.host != self._local_host:
            return self._refusal(
                StopOwnerStatus.REMOTE_HOST,
                observed,
                "The owning engine runs on a different host",
            )
        reservation = StopReservation(
            self._new_reservation_id(),
            command.record_id,
            observed.engine,
            observed.owner_fence,
        )
        cursor = conn.execute(
            "UPDATE validated_work_records SET stop_reserved_fence=?,"
            "stop_reserved_engine=?,stop_reservation_id=?,stop_reserved_at=? "
            "WHERE record_id=? AND repo_slug=? AND owner_fence=? "
            "AND owner_host=? AND owner_pid=? AND owner_started_at=? "
            "AND owner_instance_id=? AND owner_claim_hash!=''",
            (
                reservation.owner_fence,
                _encode_engine(reservation.engine),
                reservation.reservation_id,
                self._reservation_time(),
                reservation.record_id,
                self._repo_slug,
                reservation.owner_fence,
                process.host,
                process.pid,
                process.started_at,
                process.instance_id or "",
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "validated-work owner changed inside reservation transaction"
            )
        return reservation

    def release(self, conn: sqlite3.Connection, reservation: StopReservation) -> bool:
        if type(reservation) is not StopReservation:
            raise TypeError("stop reservation release requires a typed token")
        if normalize_repo_root(reservation.engine.repo_root) != self._repo_root:
            return False
        process = reservation.engine.process
        cursor = conn.execute(
            "UPDATE validated_work_records SET stop_reserved_fence=-1,"
            "stop_reserved_engine='',stop_reservation_id='',stop_reserved_at='' "
            "WHERE record_id=? AND repo_slug=? AND owner_fence=? "
            "AND owner_host=? AND owner_pid=? AND owner_started_at=? "
            "AND owner_instance_id=? AND stop_reserved_fence=? "
            "AND stop_reservation_id=? AND stop_reserved_engine=?",
            (
                reservation.record_id,
                self._repo_slug,
                reservation.owner_fence,
                process.host,
                process.pid,
                process.started_at,
                process.instance_id or "",
                reservation.owner_fence,
                reservation.reservation_id,
                _encode_engine(reservation.engine),
            ),
        )
        return cursor.rowcount == 1

    def _owner_fact(self, process: ProcessIdentity, fence: int) -> ClaimOwnerFact:
        engine = EngineIdentity(
            str(self._repo_root),
            process.instance_id,
            process.host,
            process.instance_id or "default",
            process,
        )
        availability = self._stop_availability(engine)
        if type(availability) is not EngineStopAvailability:
            raise TypeError("stop-availability owner returned an untyped result")
        return ClaimOwnerFact(engine, fence, availability)

    def _new_reservation_id(self) -> str:
        value = self._reservation_id()
        require_text(value, "generated stop reservation id")
        return value

    def _reservation_time(self) -> str:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None:
            raise TypeError("stop reservation clock must return an aware datetime")
        return value.isoformat()

    @staticmethod
    def _refusal(
        status: StopOwnerStatus,
        owner: ClaimOwnerFact | None,
        message: str,
    ) -> StopReservationRefusal:
        return StopReservationRefusal(status, owner, message)


class SqliteValidatedWorkStopReservations:
    """Control Center adapter with reservation-only authority over an existing DB."""

    def __init__(
        self,
        *,
        repo_root: Path,
        repo_slug: str,
        local_host: str,
        stop_availability: StopAvailability,
        timeout: float,
        reservation_id: Callable[[], str] = _fresh_reservation_id,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(timeout) not in {int, float} or not isfinite(timeout) or timeout <= 0:
            raise ValueError("stop reservation timeout must be finite and positive")
        self._database = state_dir(repo_root) / "validated_work.sqlite"
        self._timeout = timeout
        self._transactions = StopReservationTransactions(
            repo_root=repo_root,
            repo_slug=repo_slug,
            local_host=local_host,
            stop_availability=stop_availability,
            reservation_id=reservation_id,
            clock=clock,
        )

    def reserve_owner_stop(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopReservation | StopReservationRefusal:
        with self._write_transaction() as conn:
            return self._transactions.reserve(conn, command)

    def release_owner_stop(self, reservation: StopReservation) -> bool:
        with self._write_transaction() as conn:
            return self._transactions.release(conn, reservation)

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        database_uri = self._database.resolve().as_uri() + "?mode=rw&cache=private"
        with (
            closing(
                open_sqlite(
                    database_uri,
                    uri=True,
                    timeout=self._timeout,
                    row_factory=sqlite3.Row,
                    pragmas=False,
                )
            ) as conn,
            conn,
        ):
            # Compatibility is read before the shared durability profile because
            # changing journal mode would itself mutate an unsupported database.
            require_supported_validated_work_schema(conn)
            apply_sqlite_pragmas(
                conn,
                busy_timeout_ms=ceil(self._timeout * 1000),
            )
            conn.execute("BEGIN IMMEDIATE")
            require_supported_validated_work_schema(conn)
            yield conn


def _encode_engine(engine: EngineIdentity) -> str:
    process = engine.process
    return json.dumps(
        {
            "host": engine.host,
            "instance_id": engine.instance_id,
            "label": engine.label,
            "process": {
                "host": process.host,
                "instance_id": process.instance_id,
                "pid": process.pid,
                "started_at": process.started_at,
            },
            "repo_root": engine.repo_root,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
