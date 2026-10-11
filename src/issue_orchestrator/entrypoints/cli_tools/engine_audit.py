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
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...contracts.engine_audit import SourceStatus
from ...execution.pending_work_claim_schema import STORE_FILENAME as PENDING_WORK_CLAIMS_DB
from ...execution.providers import create_repository_host
from ...observation.engine_audit import EngineAuditInputs, Unavailable, audit_engine
from ...observation.engine_audit_diff import diff_reports, load_report
from ..bootstrap_action_liveness import ACTION_LIVENESS_DB
from ..cli_parser import add_engine_audit_arguments
from ..engine_snapshot import (
    ENGINE_LOG,
    SQLITE_TIMEOUT,
    TECH_LEAD_AUTHORITY_DB,
    TIMELINE_DB,
    VALIDATED_WORK_DB,
    snapshot_engine,
)
from .engine_audit_summary import render_summary

__all__ = [
    "ACTION_LIVENESS_DB",
    "ENGINE_LOG",
    "PENDING_WORK_CLAIMS_DB",
    "SQLITE_TIMEOUT",
    "TECH_LEAD_AUTHORITY_DB",
    "TIMELINE_DB",
    "VALIDATED_WORK_DB",
    "build_parser",
    "main",
    "run",
]


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
        _replace_file(output, payload + "\n")
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


def _replace_file(path: Path, text: str) -> None:
    """Install ``text`` at ``path`` as a new file, never writing through an existing one.

    ``os.replace`` swaps the directory entry, so an ``output`` that is a hard
    link to some other file (an engine database, say) leaves that file intact.
    """
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        staged = Path(handle.name)
    try:
        staged.write_text(text, encoding="utf-8")
        os.replace(staged, path)
    except BaseException:
        # Nothing but the report may be left beside it.
        staged.unlink(missing_ok=True)
        raise


def _inputs(state_dir: Path, scratch: Path, args: argparse.Namespace) -> EngineAuditInputs:
    return snapshot_engine(
        state_dir,
        scratch,
        repo=args.repo,
        log_tail_bytes=args.log_tail_mb * 1024 * 1024,
        github=Unavailable(SourceStatus.SKIPPED, "--no-github")
        if args.no_github
        else create_repository_host(args.repo),
    ).audit


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
