"""Cross-cutting infrastructure services bundle.

Groups services that many control-layer components need (label management,
persistence, provider resilience, timeline) into a single frozen dataclass.
This replaces 7 individual fields on ``OrchestratorDeps``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.label_store import LabelStore
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.goal_pilot_store import GoalPilotStore
    from ..ports.attempt_store import AttemptStore
    from ..ports.persistent_exchange_pair_registry import (
        PersistentExchangePairRegistry,
    )
    from ..ports.turn_mailbox import TurnMailbox
    from ..ports.needs_human_clear_store import NeedsHumanClearStore
    from ..ports.timeline_reader import TimelineReader
    from ..ports.timeline_store import TimelineStore
    from ..ports.timeline_writer import TimelineWriter
    from ..ports.triage_authority import TriageAuthorityStore
    from .background_job_supervisor import BackgroundJobSupervisor
    from .label_manager import LabelManager
    from .provider_resilience import ProviderResilienceManager


def _noop_health_check() -> None:
    """Default no-op health check for tests and disabled configurations."""


@dataclass(frozen=True)
class InfraServices:
    """Cross-cutting infrastructure services.

    Bundled into a single object so ``OrchestratorDeps`` doesn't keep growing
    one field at a time.  Backward-compat properties on OrchestratorDeps
    delegate here.
    """

    label_manager: "LabelManager"
    label_store: "LabelStore"
    queue_cache_store: "QueueCacheStore"
    provider_resilience: "ProviderResilienceManager"
    timeline_reader: "TimelineReader"
    timeline_store: "TimelineStore"
    timeline_writer: "TimelineWriter"
    goal_pilot_store: "GoalPilotStore"
    attempt_store: "AttemptStore"
    # Orchestrator-owned triage launch authority port (ADR-0031 / #6769 F2).
    triage_authority: "TriageAuthorityStore"
    # Durable provenance of orchestrator-owned stale needs-human clears (#6771 r7).
    needs_human_clear_store: "NeedsHumanClearStore"
    pair_registry: "PersistentExchangePairRegistry | None" = None
    turn_mailbox: "TurnMailbox | None" = None
    background_job_supervisor: "BackgroundJobSupervisor | None" = None
    instance_id: str = ""
    state_health_check: Callable[[], None] = field(default=_noop_health_check)
