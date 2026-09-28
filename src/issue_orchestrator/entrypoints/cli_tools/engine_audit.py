#!/usr/bin/env python3
"""``issue-orchestrator engine-audit``: a read-only outcome audit of an engine (#7490).

    issue-orchestrator engine-audit --state-dir ~/dev/porchpin/.issue-orchestrator/state \\
        --repo porchpin/porchpin --output audit.json [--previous earlier.json]

Snapshots the engine's databases (never opening them for writing), reads its
log tail and two GitHub listings, and writes the machine report
(:class:`~...contracts.engine_audit.EngineAuditReport`) as JSON plus a short
human summary. With ``--previous`` the report carries the diff: new,
resolved, persisting and unobserved anomalies.

Without ``--output`` the JSON goes to stdout and the summary to stderr, so the
report can be piped.

This is the composition root for the audit: it is the one place that opens
the snapshots through their owning stores and hands them, as read ports, to
:func:`~...observation.engine_audit.audit_engine`.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, TypeVar

from ...contracts.engine_audit import SourceStatus
from ...domain.read_only_sqlite import ReadOnlySqliteAccessError, ReadOnlySqliteFailure
from ...execution.action_liveness_store import SQLiteActionLivenessStore
from ...execution.pending_work_claim_schema import STORE_FILENAME as PENDING_WORK_CLAIMS_DB
from ...execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from ...execution.providers import create_repository_host
from ...execution.timeline_store import SqliteTimelineAuditReader
from ...infra.engine_log_reader import read_log
from ...infra.sqlite_snapshot import snapshot_sqlite
from ...infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from ...infra.validated_work_census import SqliteValidatedWorkCensus
from ...observation.engine_audit import (
    EngineAuditInputs,
    EngineLog,
    TechLeadReaders,
    Unavailable,
    audit_engine,
)
from ...observation.engine_audit_diff import diff_reports, load_report
from ..bootstrap_action_liveness import ACTION_LIVENESS_DB
from ..cli_parser import add_engine_audit_arguments
from .engine_audit_summary import render_summary

VALIDATED_WORK_DB = "validated_work.sqlite"
TECH_LEAD_AUTHORITY_DB = "tech_lead_authority.sqlite"
TIMELINE_DB = "timeline.sqlite"
ENGINE_LOG = Path("logs") / "orchestrator.log"

#: Seconds a snapshot or a snapshot read may take before it is reported unreadable.
SQLITE_TIMEOUT = 120.0

T = TypeVar("T")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue-orchestrator engine-audit",
        description="Read-only outcome audit of a Repository Engine (#7490)",
    )
    add_engine_audit_arguments(parser)
    return parser


def main(argv: list[str]) -> int:
    return run(build_parser().parse_args(argv))


def run(args: argparse.Namespace) -> int:
    """Audit with parsed ``engine-audit`` arguments (see :func:`add_engine_audit_arguments`)."""
    state_dir = Path(args.state_dir).expanduser().resolve()
    if not state_dir.is_dir():
        raise SystemExit(f"engine-audit: no state directory at {state_dir}")
    if args.window_hours <= 0 or args.log_tail_mb <= 0:
        raise SystemExit("engine-audit: --window-hours and --log-tail-mb must be positive")
    output = None if args.output is None else _output_outside(state_dir, Path(args.output))
    previous = None if args.previous is None else load_report(Path(args.previous))
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="io-engine-audit-") as scratch:
        report = audit_engine(
            _inputs(state_dir, Path(scratch), args),
            now=now,
            window=timedelta(hours=args.window_hours),
        )
    if previous is not None:
        report = report.model_copy(update={"diff": diff_reports(previous, report)})
    payload = report.model_dump_json(indent=2)
    summary = render_summary(report)
    if output is None:
        print(payload)
        print(summary, file=sys.stderr)
    else:
        output.write_text(payload + "\n", encoding="utf-8")
        print(summary)
    return 0


def _output_outside(state_dir: Path, output: Path) -> Path:
    """``output`` resolved (symlinks followed), refused if it lands in the engine's state.

    The audit never writes the engine it audits; its one write, the report,
    must not be aimed at a database or log by mistake or through a link.
    """
    resolved = output.expanduser().resolve()
    if resolved.is_relative_to(state_dir):
        raise SystemExit(
            f"engine-audit: refusing to write the report into the engine's state: {resolved}"
        )
    return resolved


def _inputs(state_dir: Path, scratch: Path, args: argparse.Namespace) -> EngineAuditInputs:
    def store(name: str, open_copy: Callable[[Path], T]) -> T | Unavailable:
        copy = _snapshot(state_dir / name, scratch / name)
        return copy if isinstance(copy, Unavailable) else open_copy(copy)

    tech_lead = store(TECH_LEAD_AUTHORITY_DB, SqliteTechLeadAuthorityStore)
    return EngineAuditInputs(
        repo=args.repo,
        state_dir=state_dir,
        validated_work=store(
            VALIDATED_WORK_DB, lambda p: SqliteValidatedWorkCensus(p, timeout=SQLITE_TIMEOUT)
        ),
        action_liveness=store(ACTION_LIVENESS_DB, SQLiteActionLivenessStore),
        tech_lead=tech_lead
        if isinstance(tech_lead, Unavailable)
        else TechLeadReaders(charter=tech_lead.charter_ledger, promotions=tech_lead),
        claims=store(PENDING_WORK_CLAIMS_DB, SqlitePendingWorkClaimStore),
        timeline=store(
            TIMELINE_DB, lambda p: SqliteTimelineAuditReader(p, timeout=SQLITE_TIMEOUT)
        ),
        log=_log(state_dir / ENGINE_LOG, tail_bytes=args.log_tail_mb * 1024 * 1024),
        github=Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else create_repository_host(args.repo),
    )


def _snapshot(live: Path, copy: Path) -> Path | Unavailable:
    """The snapshot of ``live``, or why the engine has none to take."""
    try:
        return snapshot_sqlite(live, copy, timeout=SQLITE_TIMEOUT)
    except ReadOnlySqliteAccessError as error:
        if error.reason is ReadOnlySqliteFailure.DATABASE_ABSENT:
            return Unavailable(SourceStatus.ABSENT, f"no {live.name} in the state directory")
        raise


def _log(path: Path, *, tail_bytes: int) -> EngineLog | Unavailable:
    if not path.is_file():
        return Unavailable(SourceStatus.ABSENT, f"no engine log at {path}")
    return EngineLog(read=lambda: read_log(path, tail_bytes=tail_bytes))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
