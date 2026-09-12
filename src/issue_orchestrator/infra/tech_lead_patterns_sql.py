"""SQL operations for the local Tech Lead pattern projection."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..domain.tech_lead_findings import (
    CASE_FILE_ACTIVE,
    TERMINAL_CASE_FILE_DISPOSITIONS,
    VALID_CASE_FILE_DISPOSITIONS,
    CaseFileClassification,
    CaseFileDisposition,
    PatternEvidence,
)
from typing import cast
from ..ports.tech_lead_authority import (
    TechLeadPatternConflictError,
    UnknownTechLeadPatternError,
)

def _evidence_from_row(row: sqlite3.Row) -> PatternEvidence:
    return PatternEvidence(
        signature=str(row["signature"]),
        case_file_issue_number=int(row["issue_number"]),
        observation_count=int(row["observation_count"]),
        fix_class=str(row["fix_class"]),
        area=str(row["area"]),
        diagnosis=str(row["diagnosis"]),
        disposition=cast(CaseFileDisposition, str(row["disposition"])),
    )


def _settled_disposition(
    tx: sqlite3.Connection, signature: str, disposition: str
) -> str:
    """The disposition to store, refusing to un-retire a terminal signature.

    Terminal is ABSORBING in every registry: ``admit_lifecycle_transition``
    rejects any further transition on a signature that already reached one, so
    nothing can legitimately move a row back. This projection enforces the same
    thing, which matters because the projection has paths the registries do not
    — a rolling-upgrade seed publishes local rows to shared authority WITHOUT
    their lifecycle, and the mirror that follows would otherwise write shared's
    ``active`` back over a locally retired row and make it promotable again
    (#7248 round 7 review F9).
    """
    row = tx.execute(
        "SELECT disposition FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is not None and str(row["disposition"]) in TERMINAL_CASE_FILE_DISPOSITIONS:
        return str(row["disposition"])
    return disposition


def set_disposition(
    tx: sqlite3.Connection, *, signature: str, disposition: str
) -> None:
    """Project one signature's settled lifecycle disposition."""
    if disposition not in VALID_CASE_FILE_DISPOSITIONS:
        raise ValueError(f"unknown case-file disposition {disposition!r}")
    row = tx.execute(
        "SELECT signature FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is None:
        raise UnknownTechLeadPatternError(
            f"pattern signature {signature!r} has no local case-file row"
        )
    tx.execute(
        "UPDATE tech_lead_patterns SET disposition = ? WHERE signature = ?",
        (_settled_disposition(tx, signature, disposition), signature),
    )


def record(
    tx: sqlite3.Connection,
    *,
    signature: str,
    issue_number: int,
    observation_id: str,
    fix_class: str,
    area: str,
    diagnosis: str,
) -> bool:
    """Create one signature mapping and its body observation."""
    if not observation_id.strip():
        raise ValueError(
            "record_pattern requires the identity of the observation the"
            " case-file body records"
        )
    row = tx.execute(
        "SELECT issue_number FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is not None:
        if int(row[0]) == issue_number:
            return False
        raise TechLeadPatternConflictError(
            f"pattern signature {signature!r} is already recorded for"
            f" case-file issue #{int(row[0])}"
        )
    now = datetime.now(timezone.utc).isoformat()
    tx.execute(
        "INSERT INTO tech_lead_patterns (signature, issue_number,"
        " recorded_at, observation_count, fix_class, area, diagnosis,"
        " disposition) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            signature,
            issue_number,
            now,
            1,
            fix_class,
            area,
            diagnosis,
            CASE_FILE_ACTIVE,
        ),
    )
    tx.execute(
        "INSERT INTO tech_lead_pattern_observations (signature,"
        " observation_id, recorded_at) VALUES (?, ?, ?)",
        (signature, observation_id, now),
    )
    return True


def note_observation(
    tx: sqlite3.Connection,
    *,
    signature: str,
    observation_id: str,
    fix_class: str,
    area: str,
    diagnosis: str,
) -> bool:
    """Record one observation identity and atomically merge classification."""
    if not observation_id.strip():
        raise ValueError(
            "note_pattern_observation requires a stable observation identity"
        )
    row = tx.execute(
        "SELECT observation_count, fix_class, area, diagnosis FROM"
        " tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is None:
        raise UnknownTechLeadPatternError(
            f"no pattern case file is recorded for signature {signature!r}"
        )
    # Reconcile before the create-once check so conflicts behave identically
    # on first publication and replay.
    merged = CaseFileClassification(
        fix_class=str(row["fix_class"]),
        area=str(row["area"]),
        diagnosis=str(row["diagnosis"]),
    ).merged_with(
        CaseFileClassification(
            fix_class=fix_class,
            area=area,
            diagnosis=diagnosis,
        ),
        signature=signature,
    )
    inserted = tx.execute(
        "INSERT OR IGNORE INTO tech_lead_pattern_observations (signature,"
        " observation_id, recorded_at) VALUES (?, ?, ?)",
        (signature, observation_id, datetime.now(timezone.utc).isoformat()),
    ).rowcount
    if not inserted:
        return False
    tx.execute(
        "UPDATE tech_lead_patterns SET observation_count = ?, fix_class = ?,"
        " area = ?, diagnosis = ? WHERE signature = ?",
        (
            int(row["observation_count"]) + 1,
            merged.fix_class,
            merged.area,
            merged.diagnosis,
            signature,
        ),
    )
    return True


def has_observation(
    conn: sqlite3.Connection, *, signature: str, observation_id: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tech_lead_pattern_observations WHERE signature = ?"
        " AND observation_id = ?",
        (signature, observation_id),
    ).fetchone()
    return row is not None


def list_observation_ids(
    conn: sqlite3.Connection, *, signature: str
) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT observation_id FROM tech_lead_pattern_observations"
        " WHERE signature = ? ORDER BY recorded_at, observation_id",
        (signature,),
    ).fetchall()
    return tuple(str(row["observation_id"]) for row in rows)


def mirror(
    tx: sqlite3.Connection,
    *,
    signature: str,
    issue_number: int,
    observation_ids: tuple[str, ...],
    fix_class: str,
    area: str,
    diagnosis: str,
    disposition: str,
) -> None:
    """Replace one local cache row from shared authority."""
    if issue_number <= 0 or not observation_ids:
        raise ValueError("a mirrored pattern requires an issue and observations")
    unique_ids = tuple(dict.fromkeys(observation_ids))
    now = datetime.now(timezone.utc).isoformat()
    row = tx.execute(
        "SELECT issue_number FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is not None and int(row["issue_number"]) != issue_number:
        raise TechLeadPatternConflictError(
            f"pattern signature {signature!r} is already recorded for"
            f" case-file issue #{int(row['issue_number'])}"
        )
    tx.execute(
        "INSERT INTO tech_lead_patterns (signature, issue_number, recorded_at,"
        " observation_count, fix_class, area, diagnosis, disposition)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(signature) DO UPDATE SET observation_count=excluded.observation_count,"
        " fix_class=excluded.fix_class, area=excluded.area, diagnosis=excluded.diagnosis,"
        " disposition=excluded.disposition",
        (
            signature,
            issue_number,
            now,
            len(unique_ids),
            fix_class,
            area,
            diagnosis,
            _settled_disposition(tx, signature, disposition),
        ),
    )
    tx.execute(
        "DELETE FROM tech_lead_pattern_observations WHERE signature = ?",
        (signature,),
    )
    tx.executemany(
        "INSERT INTO tech_lead_pattern_observations"
        " (signature, observation_id, recorded_at) VALUES (?, ?, ?)",
        ((signature, observation_id, now) for observation_id in unique_ids),
    )


def lookup(conn: sqlite3.Connection, *, signature: str) -> int | None:
    row = conn.execute(
        "SELECT issue_number FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    return int(row["issue_number"]) if row is not None else None


def load_evidence(
    conn: sqlite3.Connection, *, signature: str
) -> PatternEvidence | None:
    row = conn.execute(
        "SELECT signature, issue_number, observation_count, fix_class, area,"
        " diagnosis, disposition FROM tech_lead_patterns WHERE signature = ?",
        (signature,),
    ).fetchone()
    return _evidence_from_row(row) if row is not None else None


def list_patterns(conn: sqlite3.Connection) -> tuple[tuple[str, int], ...]:
    rows = conn.execute(
        "SELECT signature, issue_number FROM tech_lead_patterns ORDER BY signature",
    ).fetchall()
    return tuple((str(row["signature"]), int(row["issue_number"])) for row in rows)


def list_evidence(conn: sqlite3.Connection) -> tuple[PatternEvidence, ...]:
    rows = conn.execute(
        "SELECT signature, issue_number, observation_count, fix_class, area,"
        " diagnosis, disposition FROM tech_lead_patterns ORDER BY signature",
    ).fetchall()
    return tuple(_evidence_from_row(row) for row in rows)
