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
    from ..ports.needs_human_clear_store import NeedsHumanClearStore
    from ..ports.timeline_store import TimelineStore
    from ..ports.triage_authority import TriageAuthorityStore


def create_triage_authority_store(config: "Config") -> "TriageAuthorityStore":
    """The orchestrator-owned launch-authority adapter (#6769 F1/F2)."""
    from ..infra.triage_authority_store import SqliteTriageAuthorityStore

    return SqliteTriageAuthorityStore.for_repo(config.repo_root)


def create_needs_human_clear_store(config: "Config") -> "NeedsHumanClearStore":
    """Durable provenance of orchestrator-owned stale needs-human clears (#6771 r7).

    A recovered launch that supersedes an incomplete escalation must retry its
    stale-label removal across restarts; this durable record — not an inference
    from any active session carrying needs-human — is what proves the
    orchestrator owns the clear.
    """
    from ..execution.json_needs_human_clear_store import JsonNeedsHumanClearStore
    from ..infra.repo_identity import state_dir

    return JsonNeedsHumanClearStore(
        state_dir(config.repo_root) / "needs_human_label_clears.json"
    )


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
