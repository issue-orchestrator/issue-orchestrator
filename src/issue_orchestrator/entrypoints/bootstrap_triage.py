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

from datetime import datetime
from typing import TYPE_CHECKING

from ..infra.logging_config import get_repo_log_path, read_log_tail

if TYPE_CHECKING:
    from ..control.board_snapshot_builder import BoardSnapshotBuilder
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from ..ports.timeline_store import TimelineStore
    from ..ports.triage_authority import TriageAuthorityStore


def create_triage_authority_store(config: "Config") -> "TriageAuthorityStore":
    """The orchestrator-owned launch-authority + proposal-op adapter.

    One SQLite home for both trust records (#6769 F1/F2): the per-run launch
    authority and the gated-proposal ops keyed by proposal issue (#6778).
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


def create_board_snapshot_builder(
    config: "Config", timeline_store: "TimelineStore"
) -> "BoardSnapshotBuilder":
    """ADR-0031 §3 board-snapshot fact sources over the owned timeline store."""
    from ..control.board_snapshot_builder import BoardSnapshotBuilder

    log_path = get_repo_log_path(config.repo_root)
    return BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: timeline_store.read(issue, limit=limit),
        log_tail_provider=lambda lines: read_log_tail(log_path, lines),
        clock=datetime.now,
    )
