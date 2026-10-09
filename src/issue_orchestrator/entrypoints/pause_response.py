"""The one shape a pause/resume route replies with.

Both the control API and the web dashboard expose pause/resume, and both must
answer with what the OWNER committed rather than with what the caller asked
for — transitions are idempotent, so "the request succeeded" and "the request
changed something" are different facts. Keeping the builder here gives the two
routers one answer shape and one place where the lifecycle vocabulary is typed.
"""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Protocol

from fastapi import Request
from fastapi.responses import JSONResponse

from ..domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseTransitionOutcome,
    PauseTransitionStatus,
)


class PausableEngine(Protocol):
    """The facade methods a pause/resume route drives."""

    def pause(
        self, *, reason: PauseReason, actor: PauseActor, detail: str = ""
    ) -> PauseTransitionOutcome: ...

    def resume(
        self, *, actor: PauseActor, detail: str = ""
    ) -> PauseTransitionOutcome: ...


# Why the transitions run in a worker thread (#8222): the engine applies a
# pause or resume under its state lock, and a running tick holds that lock for
# the whole tick — tens of seconds routinely, minutes at worst. Called inline
# from an async route, that wait froze the engine's event loop: every other
# route and SSE stream stalled, and the Control Center's forward timed out
# with an empty error. In a worker thread the loop keeps serving, and the
# transition still commits when the tick releases the lock even if the caller
# has given up waiting.


async def pause_engine(
    request: Request, engine: PausableEngine, default_actor: PauseActor
) -> JSONResponse:
    """Operator pause, reported as what the owner committed."""
    actor = await requested_actor(request, default_actor)
    outcome = await asyncio.to_thread(
        partial(engine.pause, reason=PauseReason.OPERATOR, actor=actor)
    )
    return transition_response(PauseTransitionStatus.PAUSED, outcome)


async def resume_engine(
    request: Request, engine: PausableEngine, default_actor: PauseActor
) -> JSONResponse:
    """Operator resume, reported as what the owner committed."""
    actor = await requested_actor(request, default_actor)
    outcome = await asyncio.to_thread(partial(engine.resume, actor=actor))
    return transition_response(PauseTransitionStatus.RESUMED, outcome)


def transition_response(
    status: PauseTransitionStatus, outcome: PauseTransitionOutcome
) -> JSONResponse:
    """Reply with the committed transition, not the requested one.

    ``committed`` distinguishes "I paused it" from "it was already paused";
    ``actor``/``reason`` report what is actually on record, which for a rejected
    duplicate is the ORIGINAL pauser rather than this caller.
    """
    return JSONResponse(
        {
            "status": str(status),
            "committed": outcome.committed,
            "requested_actor": str(outcome.requested_actor),
            "actor": (
                str(outcome.recorded_actor)
                if outcome.recorded_actor is not None
                else None
            ),
            "reason": (
                str(outcome.recorded_reason)
                if outcome.recorded_reason is not None
                else None
            ),
        }
    )


async def requested_actor(request: Request, default: PauseActor) -> PauseActor:
    """Read the caller's self-declared actor from an optional JSON body.

    EVERY router that serves ``/api/pause`` must honour this. The Control
    Center and MCP both post to the repository engine's port, and the engine
    app registers ``web_refresh_router`` long before it mounts ``control_app``
    — so the dashboard router wins the path, and an actor honoured only by the
    control router is silently discarded. That is how a "fix" for Control
    Center attribution can journal every pause as ``web_api`` and still pass
    a client-side test.

    An unknown or absent value falls back to ``default`` rather than failing
    the request: the transition matters more than its label.
    """
    try:
        body = await request.body()
        if not body:
            return default
        data = json.loads(body)
        if not isinstance(data, dict):
            return default
        return PauseActor(str(data.get("actor", "")))
    except (json.JSONDecodeError, ValueError):
        return default
