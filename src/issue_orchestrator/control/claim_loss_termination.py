"""Stopping a session whose per-issue claim another orchestrator took.

The runtime half of the claim-loss consequence has one owner here: stop the
terminal, settle its durable pending-work claim as CONSUMED, drop the session
record and its state machine. Settlement is the #7348 fix: the facade used to
drop the record and leave the claim HELD with no live holder, and the per-tick
recovery sweep then re-admitted work this engine had just been told it no
longer owns. (Flagging the issue ``blocked:claim-lost`` stays with the facade:
control does not write labels directly.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from .in_flight_work import InFlightWorkLedger, SettlementOutcome

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from .state_machine_manager import StateMachineManager

logger = logging.getLogger(__name__)


def stop_claim_lost_session(
    session: "Session",
    *,
    state: "OrchestratorState",
    claims: "PendingWorkClaimStore",
    kill_session: Callable[[str], None],
    state_machine_manager: "StateMachineManager",
) -> None:
    """Stop ``session`` and settle its work; the claim went to another engine."""
    logger.warning("[CLAIM] Session for issue #%d lost claim - terminating", session.issue.number)
    kill_session(session.terminal_id)
    # Stopped on purpose: consume, never re-admit. Before the record goes,
    # because settlement finds the claim by the terminal that holds it.
    InFlightWorkLedger(state, claims).settle(session, SettlementOutcome.CONSUMED)
    state.drop_active_session(session.terminal_id)
    # Drop the session state machine to avoid relaunch conflicts.
    state_machine_manager.remove_session_machine(session.terminal_id)


__all__ = ["stop_claim_lost_session"]
