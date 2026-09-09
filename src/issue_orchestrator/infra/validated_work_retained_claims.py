"""Atomic snapshots of durable claims retained beyond publication work."""

import sqlite3

from ..domain.validated_work import ValidatedWorkState
from ..domain.validated_work_claim import RetainedClaim
from .validated_work_claims import owner_identity
from .validated_work_rows import current_evidence


def _require_states(states: frozenset[ValidatedWorkState]) -> None:
    if not states or any(type(state) is not ValidatedWorkState for state in states):
        raise ValueError("retained claim query requires typed states")


def retained_claims(
    conn: sqlite3.Connection, states: frozenset[ValidatedWorkState]
) -> tuple[RetainedClaim, ...]:
    _require_states(states)
    return tuple(
        candidate
        for row in conn.execute(
            "SELECT * FROM validated_work_records "
            "WHERE owner_claim_hash!='' ORDER BY record_id"
        )
        if (candidate := _candidate(conn, row, states)) is not None
    )


def retained_claim(
    conn: sqlite3.Connection,
    record_id: str,
    states: frozenset[ValidatedWorkState],
) -> RetainedClaim | None:
    _require_states(states)
    row = conn.execute(
        "SELECT * FROM validated_work_records "
        "WHERE record_id=? AND owner_claim_hash!=''",
        (record_id,),
    ).fetchone()
    return None if row is None else _candidate(conn, row, states)


def _candidate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    states: frozenset[ValidatedWorkState],
) -> RetainedClaim | None:
    state = ValidatedWorkState(row["state"])
    if state not in states:
        return None
    owner = owner_identity(row)
    assert owner is not None
    return RetainedClaim(
        row["record_id"],
        current_evidence(conn, row["record_id"]).evidence_id,
        state,
        owner,
    )
