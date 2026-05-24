"""Shared review-exchange round failure reasons and display text."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class RoundFailureReason(str, enum.Enum):
    """Machine reasons for a role round that failed before completion."""

    NO_COMPLETION = "no_completion"
    TIMEOUT = "timeout"
    PROCESS_EXITED_BEFORE_RESPONSE = "process_exited_before_response"
    INVALID_RESPONSE = "invalid_response"
    SESSION_CLOSED = "session_closed"
    PROMPT_WRITE_FAILED = "prompt_write_failed"
    ROUND_ERROR = "round_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RoundFailurePresentation:
    """Human text for one round-failure reason across all surfaces."""

    chapter_label: str
    narrative_phrase: str


_ROUND_FAILURE_PRESENTATION: dict[RoundFailureReason, RoundFailurePresentation] = {
    RoundFailureReason.NO_COMPLETION: RoundFailurePresentation(
        chapter_label="did not complete",
        narrative_phrase="did not complete",
    ),
    RoundFailureReason.TIMEOUT: RoundFailurePresentation(
        chapter_label="timed out",
        narrative_phrase="timed out",
    ),
    RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE: RoundFailurePresentation(
        chapter_label="exited before responding",
        narrative_phrase="exited before responding",
    ),
    RoundFailureReason.INVALID_RESPONSE: RoundFailurePresentation(
        chapter_label="returned invalid response",
        narrative_phrase="returned invalid response",
    ),
    RoundFailureReason.SESSION_CLOSED: RoundFailurePresentation(
        chapter_label="session was closed before responding",
        narrative_phrase="session was closed before responding",
    ),
    RoundFailureReason.PROMPT_WRITE_FAILED: RoundFailurePresentation(
        chapter_label="prompt delivery failed",
        narrative_phrase="prompt delivery failed",
    ),
    RoundFailureReason.ROUND_ERROR: RoundFailurePresentation(
        chapter_label="round failed",
        narrative_phrase="round failed",
    ),
    RoundFailureReason.UNKNOWN: RoundFailurePresentation(
        chapter_label="did not complete",
        narrative_phrase="did not complete",
    ),
}


def coerce_round_failure_reason(value: Any) -> RoundFailureReason:
    """Return a known round-failure reason, or ``UNKNOWN`` for legacy data."""
    if isinstance(value, RoundFailureReason):
        return value
    if isinstance(value, str) and value:
        try:
            return RoundFailureReason(value)
        except ValueError:
            return RoundFailureReason.UNKNOWN
    return RoundFailureReason.UNKNOWN


def round_failure_reason_value(reason: RoundFailureReason) -> str:
    """Return the stable event/artifact value for a typed reason."""
    return reason.value


def round_failure_chapter_label(reason: Any) -> str:
    """Return the label fragment used in recording chapter sidecars."""
    return _ROUND_FAILURE_PRESENTATION[
        coerce_round_failure_reason(reason)
    ].chapter_label


def round_failure_narrative_phrase(reason: Any) -> str:
    """Return the user-facing narrative phrase for a round failure."""
    return _ROUND_FAILURE_PRESENTATION[
        coerce_round_failure_reason(reason)
    ].narrative_phrase
