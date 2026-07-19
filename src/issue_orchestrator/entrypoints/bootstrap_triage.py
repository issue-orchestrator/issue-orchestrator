"""ADR-0031 triage composition helpers for the bootstrap root.

Owns construction of the triage launch-authority adapter and the
board-snapshot builder so the composition root stays within its line
budget while both stay composition-root-only concerns (control code must
never construct these — the authority store is a trust boundary and the
snapshot builder must reuse the root's OWNED timeline store; a second
store path would re-run schema init and open a write-capable connection
per read).
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from ..infra.logging_config import get_repo_log_path, read_log_tail

if TYPE_CHECKING:
    from ..control.board_snapshot_builder import BoardSnapshotBuilder
    from ..control.fact_gatherer import FactGatherer
    from ..control.triage_board import TriageBoardPublisher
    from ..domain.board_snapshot import BoardE2EHealth, SessionActivityFacts
    from ..domain.models import Session
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from ..ports import EventSink, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.timeline_store import TimelineStore
    from ..ports.triage_authority import TriageAuthorityStore
    from ..ports.working_copy import WorkingCopy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TriageComposition:
    """Dependencies that must share one authority and projection owner."""

    authority: "TriageAuthorityStore"
    board_publisher: "TriageBoardPublisher | None"
    fact_gatherer: "FactGatherer | None"


def create_triage_authority_store(config: "Config") -> "TriageAuthorityStore":
    """The orchestrator-owned trusted triage scope and ledger adapter.

    One SQLite home for launch authority, proposal ops, pattern indexes, and
    restart-safe shipped-fix memory (#6769, #6778, #6781).
    """
    from ..infra.triage_authority_store import SqliteTriageAuthorityStore

    return SqliteTriageAuthorityStore.for_repo(config.repo_root)


def wire_triage_act_executors(orchestrator: "Orchestrator") -> None:
    """Post-construction wiring for act-level triage executors (#6764/#6778).

    The executors' production runners close over live orchestrator state
    (sessions, queues, the reset pipeline), so they can only be wired once
    the orchestrator exists. The authority store is handed to the applier so
    the act-level executors can finalize gated proposals (discard ops after
    terminal handling) and the create-issue boundary can record them.
    """
    from .triage_reset_retry_wiring import (
        build_triage_kill_session_executor,
        build_triage_reset_retry_executor,
    )

    applier = orchestrator.deps.action_applier
    applier.triage_reset_retry = build_triage_reset_retry_executor(orchestrator)
    applier.triage_kill_session = build_triage_kill_session_executor(orchestrator)
    applier.triage_ops = orchestrator.deps.services.triage_authority


def create_triage_board_publisher(
    config: "Config", authority: "TriageAuthorityStore"
) -> "TriageBoardPublisher | None":
    """The fact gatherer's rung-1 triage-board projection sink (#6781).

    Gated on a configured triage agent — the board projects the triage
    ledgers (the same authority store the anchor scan classifies against),
    so with no triage agent there is nothing to project. When absent the
    fact gatherer's ``board_publisher`` stays ``None`` and publish is never
    called (no board file written, no crash). The board is a local operator
    artifact under the repo state dir, not a UI contract; it is refreshed
    each tick the anchor scan produces triage facts.
    """
    if not config.triage_review_agent:
        return None
    from ..control.triage_board import TriageBoardPublisher, triage_board_path

    return TriageBoardPublisher(
        board_path=triage_board_path(config.repo_root),
        authority=authority,
    )


def create_triage_fact_gatherer(
    config: "Config",
    repository_host: "RepositoryHost | None",
    events: "EventSink",
    authority: "TriageAuthorityStore",
    board_publisher: "TriageBoardPublisher | None",
    queue_cache_store: "QueueCacheStore | None" = None,
) -> "FactGatherer | None":
    """Wire the read-only triage ledgers and projections as one unit.

    ``queue_cache_store`` backs the tech-lead stuck sweep's durable timer +
    recovery counters (#6823); optional so the testing composition can omit it.
    """
    if repository_host is None:
        return None
    from ..control.fact_gatherer import FactGatherer
    from ..infra.e2e_runner import make_e2e_slot_reader

    return FactGatherer(
        config=config,
        repository_host=repository_host,
        events=events,
        triage_authority=authority,
        board_publisher=board_publisher,
        queue_cache_store=queue_cache_store,
        # First-class E2E workload observation feed (e2e.occupies_session_slot).
        # Always wired; a no-op that touches nothing while the flag is off.
        e2e_slot_reader=make_e2e_slot_reader(config),
    )


def create_triage_composition(
    config: "Config",
    repository_host: "RepositoryHost | None",
    events: "EventSink",
    fact_gatherer: "FactGatherer | None" = None,
    queue_cache_store: "QueueCacheStore | None" = None,
) -> TriageComposition:
    """Build the triage store and ensure both projections share one publisher."""
    authority = create_triage_authority_store(config)
    board_publisher = (
        fact_gatherer.board_publisher
        if fact_gatherer is not None
        else create_triage_board_publisher(config, authority)
    )
    if fact_gatherer is None:
        fact_gatherer = create_triage_fact_gatherer(
            config, repository_host, events, authority, board_publisher,
            queue_cache_store,
        )
    return TriageComposition(
        authority=authority,
        board_publisher=board_publisher,
        fact_gatherer=fact_gatherer,
    )


def create_board_snapshot_builder(
    config: "Config",
    timeline_store: "TimelineStore",
    board_publisher: "TriageBoardPublisher | None",
    working_copy: "WorkingCopy",
) -> "BoardSnapshotBuilder":
    """ADR-0031 §3 board-snapshot fact sources over the owned timeline store."""
    from ..control.board_snapshot_builder import BoardSnapshotBuilder

    log_path = get_repo_log_path(config.repo_root)
    return BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: timeline_store.read(issue, limit=limit),
        log_tail_provider=lambda lines: read_log_tail(log_path, lines),
        case_file_reader=board_publisher.case_files if board_publisher else lambda: (),
        shipped_fix_reader=(
            board_publisher.shipped_fixes if board_publisher else lambda _limit: ()
        ),
        e2e_health_reader=_make_e2e_health_reader(config),
        session_activity_reader=_make_session_activity_reader(working_copy),
        clock=datetime.now,
    )


def _make_session_activity_reader(
    working_copy: "WorkingCopy",
) -> "Callable[[Session], SessionActivityFacts | None]":
    """Best-effort hung-EVIDENCE probe feed for each active session (ADR-0031).

    Reads two cheap signals that tell a long-running-but-working session from a
    genuinely hung one — NEVER age alone: the mtime of the session's terminal
    recording (its agent output stream, a proxy for "last observable activity",
    the same file the quiescence detector samples) and the commit count on its
    branch ahead of base. Both are best-effort: a missing recording or an
    unreadable/absent worktree degrades that field to its unknown sentinel; the
    builder's backstop maps any unexpected error to ``None``. The health review
    reads these to corroborate a hang before proposing the GATED
    ``kill_hung_session`` (which never auto-executes).
    """
    from ..domain.board_snapshot import SessionActivityFacts

    def _read(session: "Session") -> "SessionActivityFacts | None":
        return SessionActivityFacts(
            commits_ahead=_session_commits_ahead(working_copy, session),
            last_activity_at=_recording_last_activity_iso(session),
        )

    return _read


def _recording_last_activity_iso(session: "Session") -> str | None:
    """Wall-clock ISO of the session recording's last write (mtime), else ``None``.

    The agent's output stream writes the terminal recording, so its mtime is a
    pragmatic "last observable activity" timestamp. ``None`` when the file is
    missing or unreadable (``OSError``) — the builder maps that to an unknown
    idle reading rather than a bogus "idle forever".
    """
    recording_path = session.run_assets.terminal_recording.path
    try:
        mtime = recording_path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).isoformat()


def _session_commits_ahead(working_copy: "WorkingCopy", session: "Session") -> int:
    """Commits on the session branch ahead of base, or the unknown sentinel.

    ``COMMITS_AHEAD_UNKNOWN`` when the worktree is gone or the read raises: a
    real ``0`` (no commits yet — a hang signal when paired with a high idle)
    must stay distinct from "could not read". Narrows to filesystem/subprocess/
    git errors so an unexpected failure still surfaces via the builder backstop.
    """
    from ..domain.board_snapshot import COMMITS_AHEAD_UNKNOWN
    from ..ports.git import GitError

    worktree = session.worktree_path
    try:
        if not worktree.exists():
            return COMMITS_AHEAD_UNKNOWN
        return len(working_copy.get_commits_ahead_of_main(worktree))
    except (OSError, subprocess.SubprocessError, GitError):
        return COMMITS_AHEAD_UNKNOWN


def _make_e2e_health_reader(
    config: "Config",
) -> Callable[[datetime], "BoardE2EHealth | None"]:
    """Read-only e2e-health projection feed for the board snapshot (ADR-0031).

    Reads the aggregate E2E-suite signal (cadence, red streak, chronic
    failures, quarantine) from the repo's ``e2e.db`` over a strictly read-only
    connection, plus the configured cadence/enabled flag and quarantine list.
    Best-effort: a repo with no ``e2e.db`` (or an unreadable/table-less one)
    yields ``None`` — a health review of a repo without E2E is fine.
    """
    from ..domain.board_snapshot import RECENT_E2E_RUN_WINDOW, BoardE2EHealth
    from ..infra.e2e_health_reader import read_e2e_health_facts
    from ..infra.e2e_quarantine import load_quarantine_list

    def _read(now: datetime) -> "BoardE2EHealth | None":
        db_path = config.repo_root / ".issue-orchestrator" / "e2e.db"
        if not db_path.exists():
            return None
        try:
            runs, chronic = read_e2e_health_facts(
                db_path, recent_run_limit=RECENT_E2E_RUN_WINDOW
            )
            quarantine = load_quarantine_list(
                config.repo_root / config.e2e.quarantine_file
            )
            return BoardE2EHealth.project(
                now=now,
                enabled=config.e2e.enabled,
                expected_interval_minutes=config.e2e.auto_run_interval_minutes,
                runs=runs,
                chronic_failures=chronic,
                quarantine_count=len(quarantine),
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning("[board] e2e health projection unavailable: %s", exc)
            return None

    return _read
