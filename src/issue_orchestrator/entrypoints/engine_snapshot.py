"""Open an engine's durable state on snapshots, never on the live files (#7490).

The composition root shared by ``io engine-audit`` and the improver's input
staging: the one place that byte-copies an engine's databases (see
:mod:`..infra.sqlite_snapshot`) and opens each copy through the store that owns
its schema. A database the engine does not have, or that changed under every
copy, becomes an :class:`~..observation.engine_audit.Unavailable` naming why,
never a crash and never an empty store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from ..contracts.engine_audit import SourceStatus
from ..domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure
from ..execution.action_liveness_store import (
    AUDIT_TABLES as ACTION_LIVENESS_AUDIT_TABLES,
    SQLiteActionLivenessStore,
)
from ..execution.pending_work_claim_schema import STORE_FILENAME as PENDING_WORK_CLAIMS_DB
from ..execution.pending_work_claim_store import (
    AUDIT_TABLES as CLAIM_AUDIT_TABLES,
    SqlitePendingWorkClaimStore,
)
from ..execution.timeline_store import SqliteTimelineAuditReader
from ..infra.engine_log_reader import read_log
from ..infra.sqlite_snapshot import require_tables, snapshot_sqlite
from ..infra.tech_lead_authority_store import (
    AUDIT_TABLES as TECH_LEAD_AUDIT_TABLES,
    SqliteTechLeadAuthorityStore,
)
from ..infra.tech_lead_run_record_store import (
    AUDIT_TABLES as TECH_LEAD_RUNS_AUDIT_TABLES,
    SqliteTechLeadRunRecordStore,
)
from ..infra.validated_work_census import SqliteValidatedWorkCensus
from ..observation.engine_audit import (
    EngineAuditInputs,
    EngineLog,
    TechLeadReaders,
    Unavailable,
)
from ..ports.engine_audit import OpenWorkHost
from .bootstrap_action_liveness import ACTION_LIVENESS_DB

VALIDATED_WORK_DB = "validated_work.sqlite"
TECH_LEAD_AUTHORITY_DB = "tech_lead_authority.sqlite"
TECH_LEAD_RUNS_DB = "tech_lead_runs.sqlite"
TIMELINE_DB = "timeline.sqlite"
ENGINE_LOG = Path("logs") / "orchestrator.log"

#: Seconds a snapshot or a snapshot read may take before it is reported unreadable.
SQLITE_TIMEOUT = 120.0

T = TypeVar("T")


@dataclass(frozen=True)
class EngineSnapshot:
    """An engine's stores, each opened on its own snapshot.

    ``audit`` is what :func:`~..observation.engine_audit.audit_engine` reads;
    ``tech_lead``, ``timeline`` and ``claims`` are the same opened copies, for a reader
    that needs more of them than the audit does, so every reading of one
    source comes from ONE copy of it.
    """

    audit: EngineAuditInputs
    tech_lead: SqliteTechLeadAuthorityStore | Unavailable
    timeline: SqliteTimelineAuditReader | Unavailable
    claims: SqlitePendingWorkClaimStore | Unavailable


def snapshot_engine(
    state_dir: Path,
    scratch: Path,
    *,
    repo: str,
    log_tail_bytes: int,
    github: OpenWorkHost | Unavailable,
) -> EngineSnapshot:
    """Snapshot every store the audit reads into ``scratch`` and open the copies."""
    tech_lead = snapshot_store(
        state_dir, scratch, TECH_LEAD_AUTHORITY_DB, SqliteTechLeadAuthorityStore,
        TECH_LEAD_AUDIT_TABLES,
    )
    timeline = snapshot_store(
        state_dir, scratch, TIMELINE_DB,
        lambda p: SqliteTimelineAuditReader(p, timeout=SQLITE_TIMEOUT),
    )
    claims = snapshot_store(
        state_dir, scratch, PENDING_WORK_CLAIMS_DB, SqlitePendingWorkClaimStore,
        CLAIM_AUDIT_TABLES,
    )
    audit = EngineAuditInputs(
        repo=repo,
        state_dir=state_dir,
        validated_work=snapshot_store(
            state_dir, scratch, VALIDATED_WORK_DB,
            lambda p: SqliteValidatedWorkCensus(p, timeout=SQLITE_TIMEOUT),
        ),
        action_liveness=snapshot_store(
            state_dir, scratch, ACTION_LIVENESS_DB, SQLiteActionLivenessStore,
            ACTION_LIVENESS_AUDIT_TABLES,
        ),
        tech_lead=tech_lead
        if isinstance(tech_lead, Unavailable)
        else TechLeadReaders(charter=tech_lead.charter_ledger, promotions=tech_lead),
        claims=claims,
        timeline=timeline,
        log=_log(state_dir / ENGINE_LOG, tail_bytes=log_tail_bytes),
        github=github,
    )
    return EngineSnapshot(audit=audit, tech_lead=tech_lead, timeline=timeline, claims=claims)


def snapshot_tech_lead_runs(
    state_dir: Path, scratch: Path
) -> SqliteTechLeadRunRecordStore | Unavailable:
    """The tech-lead run history, opened on its own snapshot."""
    return snapshot_store(
        state_dir, scratch, TECH_LEAD_RUNS_DB, SqliteTechLeadRunRecordStore,
        TECH_LEAD_RUNS_AUDIT_TABLES,
    )


def snapshot_store(
    state_dir: Path,
    scratch: Path,
    name: str,
    open_copy: Callable[[Path], T],
    tables: tuple[str, ...] = (),
) -> T | Unavailable:
    """``open_copy`` of a snapshot of ``state_dir/name`` holding ``tables``, or
    why the engine has none to take."""
    live = state_dir / name
    try:
        copy = require_tables(
            snapshot_sqlite(live, scratch / name, timeout=SQLITE_TIMEOUT),
            tables,
            timeout=SQLITE_TIMEOUT,
        )
    except ReadOnlySqliteAccessError as error:
        if error.reason is ReadOnlySqliteFailure.DATABASE_ABSENT:
            return Unavailable(SourceStatus.ABSENT, f"no {live.name} in the state directory")
        return Unavailable(SourceStatus.UNREADABLE, str(error))
    return open_copy(copy)


def _log(path: Path, *, tail_bytes: int) -> EngineLog | Unavailable:
    if not path.is_file():
        return Unavailable(SourceStatus.ABSENT, f"no engine log at {path}")
    return EngineLog(read=lambda: read_log(path, tail_bytes=tail_bytes))


__all__ = [
    "ENGINE_LOG",
    "EngineSnapshot",
    "SQLITE_TIMEOUT",
    "TECH_LEAD_AUTHORITY_DB",
    "TECH_LEAD_RUNS_DB",
    "TIMELINE_DB",
    "VALIDATED_WORK_DB",
    "snapshot_engine",
    "snapshot_store",
    "snapshot_tech_lead_runs",
]
