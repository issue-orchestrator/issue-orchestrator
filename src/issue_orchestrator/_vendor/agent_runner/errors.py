"""Provider error classification for agent-runner.

Classifies provider failures into coarse categories for retry/circuit logic.
"""

from __future__ import annotations

from enum import Enum


class ProviderErrorType(str, Enum):
    """Coarse provider error categories."""

    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    FATAL = "fatal"


_TRANSIENT_TOKENS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection refused",
    "connection error",
    "econnreset",
    "econnrefused",
    "enotfound",
    "eai_again",
    "gateway timeout",
    "bad gateway",
    "502",
    "503",
    "504",
    "500",
)

_RATE_LIMIT_TOKENS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
    "quota",
    "throttle",
)

_AUTH_TOKENS = (
    "unauthorized",
    "forbidden",
    "authentication",
    "invalid api key",
    "invalid_api_key",
    "401",
    "403",
)

_FATAL_TOKENS = (
    "bad request",
    "invalid request",
    "invalid argument",
    "unsupported",
    "not supported",
    "400",
)


def classify_provider_error(
    *,
    stderr: str,
    exit_code: int | None,
    timed_out: bool,
) -> ProviderErrorType | None:
    """Classify provider error based on stderr output and exit status.

    The vendored runner captures stderr via PIPE for this classification.
    Stdout flows through the PTY to ui-session.log and is not available here.
    """
    if timed_out:
        return ProviderErrorType.TRANSIENT

    text = stderr.lower()

    if any(token in text for token in _RATE_LIMIT_TOKENS):
        return ProviderErrorType.RATE_LIMIT
    if any(token in text for token in _AUTH_TOKENS):
        return ProviderErrorType.AUTH
    if any(token in text for token in _FATAL_TOKENS):
        return ProviderErrorType.FATAL
    if any(token in text for token in _TRANSIENT_TOKENS):
        return ProviderErrorType.TRANSIENT

    # Exit code-only heuristics
    if exit_code in (126, 127):
        return ProviderErrorType.FATAL

    return None
