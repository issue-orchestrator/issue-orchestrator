"""Ports of the blocked-item custody owner (#7331).

* :class:`BlockedItemCustodyReader` — what the dashboard depends on: give it
  the blocked lane's issue numbers, get every item's custody back. The
  projection never derives a state itself.
* :class:`ParkedActionReader` — the seam for the action liveness owner
  (#7350). An action that owner stopped retrying holds its item on purpose
  ("Held"). Until that owner is composed into the engine the seam is
  :data:`NO_ACTION_LIVENESS_OWNER`, which says so by name rather than passing
  for "nothing is parked".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence

from ..domain.blocked_item_custody import (
    BlockedCustodyBoard,
    BlockedItemCustody,
    CustodyState,
)


class BlockedItemCustodyReader(Protocol):
    """Custody of every item on the board's blocked lane."""

    def read(self, issue_numbers: Sequence[int]) -> BlockedCustodyBoard:
        """One custody per issue number, in the order given."""
        ...


class _NoEngineCustody:
    """No engine is installed, so nothing can hold custody of anything.

    The pre-boot dashboard (and a projection test with no engine) names this
    explicitly; a running engine always goes through its required facade
    property. Every item it is asked about is UNOWNED and says why.
    """

    def read(self, issue_numbers: Sequence[int]) -> BlockedCustodyBoard:
        return BlockedCustodyBoard(
            items=tuple(
                BlockedItemCustody(
                    issue_number=number,
                    state=CustodyState.UNOWNED,
                    reason="custody unknown: no engine is running",
                    clock=None,
                    stale_after=None,
                    stale=False,
                )
                for number in issue_numbers
            )
        )


NO_ENGINE_CUSTODY: BlockedItemCustodyReader = _NoEngineCustody()


@dataclass(frozen=True)
class ParkedActionFact:
    """One action the liveness owner parked on an item (#7350)."""

    #: The action's identity (its ``ActionType`` value).
    action: str
    #: ``permanent`` / ``needs_human`` / an exhausted ``transient``.
    outcome: str
    reason: str
    parked_since: datetime

    def __post_init__(self) -> None:
        if self.parked_since.tzinfo is None:
            raise ValueError("a parked action needs an aware timestamp")


class ParkedActionReader(Protocol):
    """Actions the liveness owner stopped retrying, per issue."""

    def parked_for_issue(self, issue_number: int) -> tuple[ParkedActionFact, ...]:
        ...


class _NoActionLivenessOwner:
    """The engine composes no action liveness owner yet (#7350 is in flight).

    Deliberately not a stand-in store: nothing CAN be parked while no owner
    parks anything, so an empty answer is the truth, not a default.
    """

    def parked_for_issue(self, issue_number: int) -> tuple[ParkedActionFact, ...]:
        del issue_number
        return ()


NO_ACTION_LIVENESS_OWNER: ParkedActionReader = _NoActionLivenessOwner()


__all__ = [
    "NO_ACTION_LIVENESS_OWNER",
    "NO_ENGINE_CUSTODY",
    "BlockedItemCustodyReader",
    "ParkedActionFact",
    "ParkedActionReader",
]
