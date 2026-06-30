"""Telemetry for the main orchestration tick.

Owns the "this tick overran the heartbeat budget" signal. A slow tick is the
direct cause of the dashboard flipping every card to "stalled": the heartbeat
(`last_tick_completed_at`) stops advancing while the loop is busy, and the stale
detector infers a stall from heartbeat age. A real incident had the loop frozen
153.9s running a synchronous publish (validation suite + git push + PR create)
for one issue, during which the whole board went stale.

The orchestrator already logged "[LOOP] Tick took ..." for humans, but that is
log-only — the UI/timeline could not react to it and could only name the coarse
`current_tick_phase`. This module promotes the signal to a machine event
carrying the sub-phase attribution, while keeping the human log line.
"""

import logging
from typing import TYPE_CHECKING

from ..events import EventName, EventContext
from ..ports import EventSink, make_trace_event

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState

logger = logging.getLogger(__name__)

# Wall-clock budget for a single tick. Above this the heartbeat is visibly
# lagging and the tick is worth flagging. Matches the long-standing "[LOOP]
# Tick took" warning threshold.
SLOW_TICK_SECONDS = 10.0


def report_slow_tick(
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    tick_elapsed: float,
    active_elapsed: float,
) -> None:
    """Log and emit a machine event when a tick overruns the heartbeat budget.

    ``active_elapsed`` is measured precisely; the dominant phase is attributed to
    active-session processing (synchronous completion/publish) versus the rest of
    the tick (planning + fetch) so a consumer can tell a slow publish apart from a
    slow planning cycle without parsing logs.
    """
    if tick_elapsed <= SLOW_TICK_SECONDS:
        return

    logger.warning("[LOOP] Tick took %.1fs", tick_elapsed)
    dominant_phase = "active_sessions" if active_elapsed * 2 >= tick_elapsed else "planning"
    events.publish(make_trace_event(
        EventName.TICK_SLOW,
        event_context.enrich({
            "duration_seconds": round(tick_elapsed, 1),
            "active_seconds": round(active_elapsed, 1),
            "dominant_phase": dominant_phase,
            "active_sessions": len(state.active_sessions),
        }),
    ))
