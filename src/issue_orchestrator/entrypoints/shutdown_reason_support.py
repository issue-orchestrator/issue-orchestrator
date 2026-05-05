"""Shared parsing for shutdown ``reason`` / ``actor`` on HTTP shutdown endpoints.

Every shutdown route in this codebase rejects unreasoned shutdowns
because the orchestrator log used to record only "Received shutdown
signal" with no source — operators couldn't tell whether the trigger
was a CLI, the cc, a browser tab, or an external SIGTERM. The
validation rule lives here so adding/changing it touches one place;
route handlers just consume the parsed result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.responses import JSONResponse


@dataclass(frozen=True)
class ShutdownReason:
    """The validated reason/actor pair extracted from a shutdown request body."""

    reason: str
    actor: str


def parse_shutdown_reason(
    body: Any,
    *,
    endpoint: str,
    default_actor: str,
) -> ShutdownReason | JSONResponse:
    """Validate ``reason`` (and optional ``actor``) on a shutdown request body.

    ``body`` is whatever ``await request.json()`` returned; non-dicts
    are tolerated and surface as "missing reason" so a malformed body
    gets the same 400 the contract promises rather than a 500.

    Returns either a :class:`ShutdownReason` or a 400 ``JSONResponse``
    that the caller should return verbatim.
    """
    if not isinstance(body, dict):
        body = {}
    raw_reason = body.get("reason")
    reason = raw_reason.strip() if isinstance(raw_reason, str) else ""
    if not reason:
        return JSONResponse(
            {
                "error": "reason is required",
                "hint": (
                    f"POST {endpoint} requires a non-empty 'reason' "
                    "in the JSON body so each shutdown is traceable in "
                    "the orchestrator log."
                ),
            },
            status_code=400,
        )
    raw_actor = body.get("actor")
    actor = raw_actor.strip() if isinstance(raw_actor, str) else ""
    return ShutdownReason(reason=reason, actor=actor or default_actor)


__all__ = ["ShutdownReason", "parse_shutdown_reason"]
