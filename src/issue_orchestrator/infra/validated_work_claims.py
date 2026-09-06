"""Record ownership: secret, generation, full process identity and positive death proof."""

from __future__ import annotations

import hmac
import sqlite3
from datetime import UTC, datetime

from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_claim import (
    ClaimSecret,
    ProcessIdentity,
    ValidatedWorkClaim,
)
from ..ports.validated_work_verification import OrchestratorLivenessPort
from .validated_work_rows import current_evidence, record_row


def owner_identity(row: sqlite3.Row) -> ProcessIdentity | None:
    if not row["owner_claim_hash"]:
        return None
    return ProcessIdentity(
        row["owner_host"],
        row["owner_pid"],
        row["owner_started_at"],
        row["owner_instance_id"] or None,
    )


class ClaimAuthority:
    """Constructed with a trusted liveness capability, never supplied per request."""

    def __init__(self, liveness: OrchestratorLivenessPort) -> None:
        self._liveness = liveness

    def acquire(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        expected_states: frozenset[ValidatedWorkState],
        evidence_id: str,
    ) -> ValidatedWorkClaim | None:
        if not expected_states or any(
            not isinstance(state, ValidatedWorkState) for state in expected_states
        ):
            raise ValueError("claim acquisition requires typed expected states")
        row = record_row(conn, record_id)
        if (
            row["state"] not in expected_states
            or current_evidence(conn, record_id).evidence_id != evidence_id
        ):
            return None
        current = self._liveness.current()
        if not isinstance(current, ProcessIdentity):
            raise TypeError("liveness must supply a process identity")
        owner = owner_identity(row)
        if owner is not None and not self._may_replace(owner, current):
            return None
        secret = ClaimSecret()
        fence = row["owner_fence"] + 1
        conn.execute(
            "UPDATE validated_work_records SET owner_fence=?,owner_host=?,owner_pid=?,owner_started_at=?,"
            "owner_claim_hash=?,owner_instance_id=?,owner_claimed_at=?,stop_reserved_fence=-1,stop_reserved_engine='',"
            "stop_reservation_id='',stop_reserved_at='' WHERE record_id=? AND owner_fence=?",
            (
                fence,
                current.host,
                current.pid,
                current.started_at,
                secret.digest(),
                current.instance_id or "",
                datetime.now(UTC).isoformat(),
                record_id,
                row["owner_fence"],
            ),
        )
        return ValidatedWorkClaim(record_id, fence, secret, current)

    def _may_replace(self, owner: ProcessIdentity, current: ProcessIdentity) -> bool:
        if owner == current or owner.host != current.host:
            return False
        dead = self._liveness.is_provably_dead(owner)
        if type(dead) is not bool:
            raise TypeError("death proof must be bool")
        return dead

    def holds(self, conn: sqlite3.Connection, claim: ValidatedWorkClaim) -> bool:
        row = conn.execute(
            "SELECT * FROM validated_work_records WHERE record_id=?", (claim.record_id,)
        ).fetchone()
        return bool(
            row is not None
            and row["owner_fence"] == claim.fence
            and hmac.compare_digest(row["owner_claim_hash"], claim.secret.digest())
            and owner_identity(row) == claim.owner
            and self._liveness.current() == claim.owner
        )

    def relinquish(self, conn: sqlite3.Connection, claim: ValidatedWorkClaim) -> bool:
        if not self.holds(conn, claim):
            return False
        row = record_row(conn, claim.record_id)
        if row["stop_reserved_fence"] == claim.fence:
            return False
        conn.execute(
            "UPDATE validated_work_records SET owner_fence=owner_fence+1,owner_host='',owner_pid=0,owner_started_at='',"
            "owner_claim_hash='',owner_instance_id='',owner_claimed_at='' WHERE record_id=? AND owner_fence=?",
            (claim.record_id, claim.fence),
        )
        return True
