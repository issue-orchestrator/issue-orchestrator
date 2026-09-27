"""Where a work item stalled, from the events the engine published about it.

Only event NAMES and a few payload fields that exist at every commit the exam
targets are read, so the same derivation works against an older engine. The
refusing gate is not derived here: the harness asks the engine's own gate
owner (``control.review_validity``) about the item's final GitHub state and
passes the answer in, so the exam never re-implements gate policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .observation import StallFacts

#: Round failures where the agent never took the prompt — something on its
#: screen (a trust dialog, an update prompt) was waiting for an answer
#: (``domain/review_exchange_failures.py``).
_SCREEN_FAILURES = frozenset({"prompt_not_accepted", "prompt_write_failed"})

#: Events that say nothing about the item's progress.
_NOISE_PREFIXES = ("tick.", "labels.mutation_summary", "queue.", "orchestrator.")


@dataclass(frozen=True)
class ItemEvent:
    name: str
    at: str
    payload: Mapping[str, Any]

    @classmethod
    def from_stream(cls, raw: Mapping[str, Any]) -> "ItemEvent":
        """Adapt one control-API event (``{type, payload, ...}``)."""
        name = raw.get("type")
        if not isinstance(name, str) or not name:
            raise ValueError(f"event without a type: {raw!r}")
        payload = raw.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        at = payload.get("timestamp") or raw.get("timestamp") or ""
        return cls(name=name, at=str(at), payload=payload)


def concerns_item(
    raw: Mapping[str, Any],
    *,
    issue_keys: frozenset[str],
    issue_number: int,
    pr_numbers: frozenset[int],
) -> bool:
    """Whether one stream event is about this work item.

    The engine keys an item's events inconsistently: by issue number
    (``"7305"``), by the title's external id (``"M0-760"``), and — for review
    events — by the PR number (``"7306"``). Payloads that name the item carry
    ``issue_number`` / ``pr_number``. Any of those identifies the item.
    """
    key = raw.get("issue_key")
    if isinstance(key, str) and (key in issue_keys or _is_number(key, pr_numbers)):
        return True
    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        return False
    return _as_int(payload.get("issue_number")) == issue_number or (
        _as_int(payload.get("pr_number")) in pr_numbers
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _is_number(key: str, numbers: frozenset[int]) -> bool:
    return _as_int(key) in numbers


def unanswered_screen(events: Sequence[ItemEvent]) -> str:
    """The last agent that never took its prompt, if any.

    From the engine's side an unanswered dialog (Codex 0.156's "Folder
    access") and an agent that died before reading are the same signal —
    the prompt write or acceptance failed — so the report says what is known
    rather than guessing which.
    """
    for event in reversed(events):
        if event.name != "review_exchange.role_timeout":
            continue
        failure = str(event.payload.get("failure_reason") or "")
        if failure in _SCREEN_FAILURES:
            role = event.payload.get("role") or "agent"
            composer = event.payload.get("composer_state")
            suffix = f", composer {composer}" if composer else ""
            return f"{role} never took its prompt ({failure}{suffix})"
    return ""


def stall_facts(
    events: Sequence[ItemEvent],
    *,
    refusing_gate: str,
    blocking_labels: Sequence[str],
    parked_screen: str,
) -> StallFacts:
    """``parked_screen`` is a live session of the item sitting silent on a
    screen (see :mod:`.screens`); the engine publishes no event for that, so
    it is only reported when the engine's own round-failure signal is absent.
    """
    progress = [e for e in events if not e.name.startswith(_NOISE_PREFIXES)]
    last = progress[-1] if progress else None
    return StallFacts(
        last_transition=last.name if last else "",
        last_transition_at=last.at if last else "",
        refusing_gate=refusing_gate,
        blocking_labels=tuple(sorted(blocking_labels)),
        unanswered_screen=unanswered_screen(events) or parked_screen,
    )
