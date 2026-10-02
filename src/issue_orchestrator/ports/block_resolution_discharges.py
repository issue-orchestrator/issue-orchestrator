"""Port: the write-ahead record of a resolution's discharge (#7658).

A ``resolve_block`` discharges the needs-human causes it decided, then
requeues or closes its item. The action that carries it is replayed until it
succeeds (or its approved proposal is finalized), so on a replay the executor
must know whether the discharge already happened: discharging again would
clear a block raised AFTER the first one (an agent's new question, or a stuck
sweep giving up again), which only the operator may clear.

So the discharge is bracketed, keyed by the decision's identity
(``<run id>/<action id>``): ``begin`` is durable BEFORE the shared block is
touched, ``commit`` once it settled, ``abandon`` when it settled without
discharging anything. A replay that finds it committed only finishes (makes
the split's children runnable, posts the discharge markers, requeues or
closes); one that finds it begun cannot tell a discharge that happened from
a block raised since, so it hands the item back and discharges nothing.

Each record also names the item and the causes, so "has the tech lead ever
discharged this cause on this item?" (the reversibility rule) is answered
from here as well as from the item's markers on GitHub, across a crash
between the discharge and the marker comment.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.operator_decision_retry import DecisionRetryState


class BlockResolutionDischarges(Protocol):
    """Durable per-decision discharge state of a ``resolve_block``."""

    def begin_block_resolution(
        self, *, decision_id: str, issue_number: int, causes: frozenset[str]
    ) -> None: ...

    def commit_block_resolution(self, *, decision_id: str) -> None: ...

    def abandon_block_resolution(self, *, decision_id: str) -> None:
        """The discharge settled without discharging; a replay decides afresh."""
        ...

    def block_resolution_state(self, *, decision_id: str) -> DecisionRetryState | None: ...

    def resolved_causes(self, *, issue_number: int) -> dict[str, frozenset[str]]:
        """Cause value -> the decisions that began or committed its discharge on the item."""
        ...


__all__ = ["BlockResolutionDischarges", "DecisionRetryState"]
