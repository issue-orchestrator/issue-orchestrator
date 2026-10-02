"""Port: the blocked items a health review must triage (#7593).

The session launcher writes a health review's triage agenda into its
``tech-lead-data/`` and records the agenda's grants in the run's launch
authority, but it never holds ``OrchestratorState`` itself. The composition
root wires ``control.blocked_item_triage.StateBlockedItemTriage``; launches run
inside the tick under the state lock, so its read of live state is safe. It
makes no GitHub call.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.blocked_item_triage import TriageAgenda


@runtime_checkable
class BlockedItemTriageAgenda(Protocol):
    def agenda(self, *, anchor_issue_number: int) -> TriageAgenda:
        """Every blocked item owed a triage this run (never the anchor itself)."""
        ...


class NoBlockedItemTriage:
    """Null object: no blocked item is owed a triage (tests, non-engine roots)."""

    def agenda(self, *, anchor_issue_number: int) -> TriageAgenda:
        del anchor_issue_number
        return TriageAgenda()


NO_BLOCKED_ITEM_TRIAGE: BlockedItemTriageAgenda = NoBlockedItemTriage()
