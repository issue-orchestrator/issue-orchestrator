"""Why a Control Center command to a Repository Engine failed (#8222).

The Control Center forwards operator commands — pause, resume, refresh, and
tech-lead proposal decisions — to the engine's loopback API. Every such
forward used to reduce a failure to ``str(exc)``, and an ``httpx.ReadTimeout``
stringifies to ``""``: the operator saw ``{"error": "passthrough_failed",
"detail": ""}`` while the engine was simply busy. This module is the one place
that turns a failed forward into a typed, never-empty account of what happened,
so every forwarding path reports the same facts the same way.

The engine applies each of these commands while holding its state lock, and a
running tick holds that lock for the whole tick. Ticks routinely last tens of
seconds and occasionally minutes, so a command can wait on the engine for that
long before it is applied. ``ENGINE_COMMAND_TIMEOUT_SECONDS`` is sized for that
wait rather than for network latency.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


#: How long the Control Center waits for an engine to apply one command.
#:
#: Sized from a live engine's tick log: about 38% of ticks took over 10s (the
#: old budget, which therefore failed routinely), about 1.6% over 60s, and
#: about 0.1% over 120s. A command that outlasts this is still delivered — the
#: engine applies it off its event loop once the tick releases the state lock —
#: so the timeout failure says the outcome is unknown rather than refused.
ENGINE_COMMAND_TIMEOUT_SECONDS = 120.0

#: Upstream bodies are echoed for diagnosis, bounded so an HTML error page or a
#: traceback cannot bloat the operator-facing payload.
UPSTREAM_BODY_EXCERPT_CHARS = 2000

#: How much of an upstream body the operator-facing ``detail`` quotes. The
#: detail lands in a toast, so it names the cause in a sentence; the longer
#: ``upstream_body`` field keeps the rest for diagnosis.
DETAIL_BODY_EXCERPT_CHARS = 300


class EngineCommandFailureKind(StrEnum):
    """The distinguishable ways a forwarded engine command can fail."""

    #: Nothing was sent: connection refused, DNS, a connect or pool timeout.
    UNREACHABLE = "unreachable"
    #: The whole request was sent and the engine did not answer in time.
    NO_ANSWER = "no_answer"
    #: The exchange broke off mid-way (write timeout, read error, dropped
    #: connection): the engine may or may not have received the command.
    INTERRUPTED = "interrupted"
    #: The engine answered with a non-success HTTP status.
    UPSTREAM_ERROR = "upstream_error"
    #: The engine answered success with a body that is not a JSON object.
    INVALID_BODY = "invalid_body"


_HTTP_STATUS_BY_KIND: dict[EngineCommandFailureKind, int] = {
    EngineCommandFailureKind.UNREACHABLE: 502,
    EngineCommandFailureKind.NO_ANSWER: 504,
    EngineCommandFailureKind.INTERRUPTED: 502,
    EngineCommandFailureKind.UPSTREAM_ERROR: 502,
    EngineCommandFailureKind.INVALID_BODY: 502,
}


@dataclass(frozen=True)
class EngineCommandFailure:
    """One failed engine command, with a cause the operator can act on."""

    kind: EngineCommandFailureKind
    command: str
    url: str
    detail: str
    upstream_status: int | None = None
    upstream_body: str | None = None

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("EngineCommandFailure.detail must say what went wrong")

    @property
    def http_status(self) -> int:
        """The Control Center's own HTTP status for this failure."""
        return _HTTP_STATUS_BY_KIND[self.kind]

    def to_payload(self) -> dict[str, Any]:
        """The ``passthrough_failed`` response body."""
        return {
            "error": "passthrough_failed",
            "failure": str(self.kind),
            "command": self.command,
            "detail": self.detail,
            "upstream_status": self.upstream_status,
            "upstream_body": self.upstream_body,
        }


def _excerpt(text: str, limit: int = UPSTREAM_BODY_EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _quoted(text: str) -> str:
    """A body as the detail sentence quotes it."""
    return _excerpt(text, DETAIL_BODY_EXCERPT_CHARS) or "(empty body)"


def exception_text(exc: BaseException) -> str:
    """``Type: message`` — never empty, unlike ``str(exc)`` for httpx timeouts."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


# The failure builders below are transport-agnostic: the HTTP adapter
# (``orchestrator_http_api``, the one execution module allowed to import
# httpx) maps its exceptions onto them.


def refused_failure(
    *, command: str, url: str, upstream_status: int, body_text: str
) -> EngineCommandFailure:
    """The engine answered with a non-success HTTP status."""
    body = _excerpt(body_text)
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.UPSTREAM_ERROR,
        command=command,
        url=url,
        detail=(
            f"Engine refused {command} at {url} with HTTP "
            f"{upstream_status}: {_quoted(body_text)}"
        ),
        upstream_status=upstream_status,
        upstream_body=body,
    )


def unreachable_failure(*, command: str, url: str, cause: str) -> EngineCommandFailure:
    """Nothing answered: connection refused, DNS, or a connect timeout."""
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.UNREACHABLE,
        command=command,
        url=url,
        detail=f"Could not reach the engine at {url} for {command}: {cause}",
    )


def unanswered_failure(
    *, command: str, url: str, timeout_seconds: float, cause: str
) -> EngineCommandFailure:
    """The engine took the request but did not answer within the budget."""
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.NO_ANSWER,
        command=command,
        url=url,
        detail=(
            f"Engine did not answer {command} at {url} within "
            f"{timeout_seconds:g}s ({cause}). The engine applies "
            f"{command} once its current tick releases the state lock, so it may "
            "still take effect; read the engine state before retrying."
        ),
    )


def interrupted_failure(*, command: str, url: str, cause: str) -> EngineCommandFailure:
    """The exchange broke off after it started; delivery is unknown."""
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.INTERRUPTED,
        command=command,
        url=url,
        detail=(
            f"The exchange with the engine at {url} for {command} broke off ({cause}). "
            f"The engine may or may not have received {command}; read the engine "
            "state before retrying."
        ),
    )


def undecodable_failure(
    *, command: str, url: str, reason: str, body_text: str
) -> EngineCommandFailure:
    """The engine answered with a body that is not JSON."""
    body = _excerpt(body_text)
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.INVALID_BODY,
        command=command,
        url=url,
        detail=(
            f"Engine answered {command} at {url} with a body that is not JSON "
            f"({reason}): {_quoted(body_text)}"
        ),
        upstream_body=body,
    )


def engine_answer_failure(
    answer: object, *, command: str, url: str, accepted: str
) -> EngineCommandFailure | None:
    """Why a 2xx answer is not the command's own answer, or ``None`` if it is.

    Every forwarded engine command answers an object whose ``status`` names
    what the engine did (``paused``, ``resumed``, ``refresh_requested``). A
    stale port now served by something else can answer 200 with any object;
    that must not read as success (#8222 r2 F2).
    """
    if not isinstance(answer, dict):
        return non_object_body_failure(answer, command=command, url=url, upstream_status=200)
    if answer.get("status") == accepted:
        return None
    return off_contract_answer_failure(
        command=command, url=url, upstream_status=200, body_text=json.dumps(answer)
    )


def off_contract_answer_failure(
    *, command: str, url: str, upstream_status: int, body_text: str
) -> EngineCommandFailure:
    """A JSON answer the caller's contract does not accept.

    A refusal (4xx/5xx) keeps its HTTP status and body as the cause — an auth
    or validation failure must say so (#8222 r1 F2). A success-status answer
    off the contract is an invalid body.
    """
    if upstream_status >= 400:
        return refused_failure(
            command=command, url=url, upstream_status=upstream_status, body_text=body_text
        )
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.INVALID_BODY,
        command=command,
        url=url,
        detail=(
            f"Engine answered {command} at {url} (HTTP {upstream_status}) with an "
            f"answer it does not define: {_quoted(body_text)}"
        ),
        upstream_status=upstream_status,
        upstream_body=_excerpt(body_text),
    )


def non_object_body_failure(
    body: object, *, command: str, url: str, upstream_status: int
) -> EngineCommandFailure:
    """An engine answer that decoded as JSON but is not an object."""
    text = _excerpt(json.dumps(body))
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.INVALID_BODY,
        command=command,
        url=url,
        detail=(
            f"Engine answered {command} at {url} (HTTP {upstream_status}) with JSON "
            f"that is not an object: {_quoted(text)}"
        ),
        upstream_status=upstream_status,
        upstream_body=text,
    )


__all__ = [
    "ENGINE_COMMAND_TIMEOUT_SECONDS",
    "EngineCommandFailure",
    "EngineCommandFailureKind",
    "engine_answer_failure",
    "exception_text",
    "interrupted_failure",
    "non_object_body_failure",
    "off_contract_answer_failure",
    "refused_failure",
    "unanswered_failure",
    "undecodable_failure",
    "unreachable_failure",
]
