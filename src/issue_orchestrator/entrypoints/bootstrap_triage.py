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

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ..infra.logging_config import get_repo_log_path, read_log_tail

if TYPE_CHECKING:
    from ..control.board_snapshot_builder import BoardSnapshotBuilder
    from ..control.fact_gatherer import FactGatherer
    from ..control.triage_board import TriageBoardPublisher
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from ..ports import EventSink, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.timeline_store import TimelineStore
    from ..ports.triage_authority import TriageAuthorityStore


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

    return FactGatherer(
        config=config,
        repository_host=repository_host,
        events=events,
        triage_authority=authority,
        board_publisher=board_publisher,
        queue_cache_store=queue_cache_store,
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
        clock=datetime.now,
    )
