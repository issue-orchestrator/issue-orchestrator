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
from collections.abc import Callable
from typing import Any

import httpx

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


class EngineCommandFailureKind(StrEnum):
    """The distinguishable ways a forwarded engine command can fail."""

    #: Nothing answered: connection refused, DNS, or a connect timeout.
    UNREACHABLE = "unreachable"
    #: The engine accepted the request but did not answer in time.
    NO_ANSWER = "no_answer"
    #: The engine answered with a non-success HTTP status.
    UPSTREAM_ERROR = "upstream_error"
    #: The engine answered success with a body that is not a JSON object.
    INVALID_BODY = "invalid_body"


_HTTP_STATUS_BY_KIND: dict[EngineCommandFailureKind, int] = {
    EngineCommandFailureKind.UNREACHABLE: 502,
    EngineCommandFailureKind.NO_ANSWER: 504,
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


def _excerpt(text: str) -> str:
    if len(text) <= UPSTREAM_BODY_EXCERPT_CHARS:
        return text
    return text[:UPSTREAM_BODY_EXCERPT_CHARS] + "…[truncated]"


def _exception_text(exc: BaseException) -> str:
    """``Type: message`` — never empty, unlike ``str(exc)`` for httpx timeouts."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


@dataclass(frozen=True)
class _FailedForward:
    """What a classifier needs to describe one failed forward."""

    command: str
    url: str
    timeout_seconds: float


def _refused(exc: httpx.HTTPStatusError, ctx: _FailedForward) -> EngineCommandFailure:
    body = _excerpt(exc.response.text)
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.UPSTREAM_ERROR,
        command=ctx.command,
        url=ctx.url,
        detail=(
            f"Engine refused {ctx.command} at {ctx.url} with HTTP "
            f"{exc.response.status_code}: {body or '(empty body)'}"
        ),
        upstream_status=exc.response.status_code,
        upstream_body=body,
    )


def _unreachable(exc: httpx.HTTPError, ctx: _FailedForward) -> EngineCommandFailure:
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.UNREACHABLE,
        command=ctx.command,
        url=ctx.url,
        detail=f"Could not reach the engine at {ctx.url} for {ctx.command}: {_exception_text(exc)}",
    )


def _unanswered(exc: httpx.HTTPError, ctx: _FailedForward) -> EngineCommandFailure:
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.NO_ANSWER,
        command=ctx.command,
        url=ctx.url,
        detail=(
            f"Engine did not answer {ctx.command} at {ctx.url} within "
            f"{ctx.timeout_seconds:g}s ({_exception_text(exc)}). The engine applies "
            f"{ctx.command} once its current tick releases the state lock, so it may "
            "still take effect; read the engine state before retrying."
        ),
    )


def _undecodable(exc: json.JSONDecodeError, ctx: _FailedForward) -> EngineCommandFailure:
    body = _excerpt(exc.doc)
    return EngineCommandFailure(
        kind=EngineCommandFailureKind.INVALID_BODY,
        command=ctx.command,
        url=ctx.url,
        detail=(
            f"Engine answered {ctx.command} at {ctx.url} with a body that is not JSON "
            f"({exc.msg}): {body or '(empty body)'}"
        ),
        upstream_body=body,
    )


# First match wins, so the specific httpx classes precede their bases: a
# connect timeout means nothing answered, not that the engine was slow.
_CLASSIFIERS: tuple[tuple[tuple[type[Exception], ...], Callable[[Any, _FailedForward], EngineCommandFailure]], ...] = (
    ((httpx.HTTPStatusError,), _refused),
    ((httpx.ConnectError, httpx.ConnectTimeout), _unreachable),
    ((httpx.TimeoutException,), _unanswered),
    ((json.JSONDecodeError,), _undecodable),
    ((httpx.HTTPError,), _unreachable),
)


def describe_engine_command_failure(
    exc: httpx.HTTPError | json.JSONDecodeError,
    *,
    command: str,
    url: str,
    timeout_seconds: float,
) -> EngineCommandFailure:
    """Classify one failed forward.

    Only transport, HTTP-status and body-decoding failures are accepted: any
    other exception is a bug in the Control Center, and the caller must let it
    propagate rather than dress it up as an engine failure.
    """
    ctx = _FailedForward(command=command, url=url, timeout_seconds=timeout_seconds)
    for exception_types, describe in _CLASSIFIERS:
        if isinstance(exc, exception_types):
            return describe(exc, ctx)
    raise TypeError(f"not an engine command failure: {type(exc).__name__}")


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
            f"that is not an object: {text}"
        ),
        upstream_status=upstream_status,
        upstream_body=text,
    )


__all__ = [
    "ENGINE_COMMAND_TIMEOUT_SECONDS",
    "EngineCommandFailure",
    "EngineCommandFailureKind",
    "describe_engine_command_failure",
    "non_object_body_failure",
]
