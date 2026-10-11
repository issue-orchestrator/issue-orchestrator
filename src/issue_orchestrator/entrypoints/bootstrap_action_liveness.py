"""Composition of the action liveness owner (#7350)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..control.action_liveness import ActionLivenessOwner
from ..control.action_liveness_escalation import ActionLivenessEscalation
from ..control.planned_action_liveness import PlannedActionLiveness
from ..execution.action_liveness_store import SQLiteActionLivenessStore
from ..infra.repo_identity import state_dir
from ..infra.runtime_identity import resolve_runtime_identity

if TYPE_CHECKING:
    from ..control.action_results import SupportsApplyAction
    from ..control.label_manager import LabelManager
    from ..infra.config import Config
    from ..ports.event_sink import EventSink
    from ..ports.tech_lead_charter_ledger import TechLeadCharterLedger

#: The owner's own database, registered in ``infra.sqlite_registry``.
ACTION_LIVENESS_DB = "action_liveness.sqlite"


def action_liveness_store(config: "Config") -> SQLiteActionLivenessStore:
    """The owner's durable rows; readers (the tech-lead board) open their own."""
    return SQLiteActionLivenessStore(state_dir(config.repo_root) / ACTION_LIVENESS_DB)


def build_action_liveness(
    config: "Config",
    *,
    events: "EventSink",
    action_applier: "SupportsApplyAction",
    label_manager: "LabelManager",
    charter: "TechLeadCharterLedger",
) -> PlannedActionLiveness:
    """One owner per engine: the planner gate and every other path share it.

    ``charter`` is where a park of an executed tech-lead decision's effect is
    linked back to its record (#7362).
    """
    owner = ActionLivenessOwner(
        store=action_liveness_store(config),
        escalation=ActionLivenessEscalation(
            events=events,
            applier=action_applier,
            needs_human_label=label_manager.needs_human,
        ),
        # The code a park is decided under (#7592): an engine on a newer commit
        # re-tries each such park once.
        engine_commit=resolve_runtime_identity().source_commit_sha,
    )
    return PlannedActionLiveness(
        owner, escalation_label=label_manager.needs_human, charter=charter
    )


__all__ = ["ACTION_LIVENESS_DB", "action_liveness_store", "build_action_liveness"]
