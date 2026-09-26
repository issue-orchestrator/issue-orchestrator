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


def unanswered_screen(events: Sequence[ItemEvent]) -> str:
    """The last agent screen that never took its prompt, if any."""
    for event in reversed(events):
        if event.name != "review_exchange.role_timeout":
            continue
        failure = str(event.payload.get("failure_reason") or "")
        if failure in _SCREEN_FAILURES:
            role = event.payload.get("role") or "agent"
            composer = event.payload.get("composer_state")
            suffix = f", composer {composer}" if composer else ""
            return f"{role}: {failure}{suffix}"
    return ""


def stall_facts(
    events: Sequence[ItemEvent],
    *,
    refusing_gate: str,
    blocking_labels: Sequence[str],
) -> StallFacts:
    progress = [e for e in events if not e.name.startswith(_NOISE_PREFIXES)]
    last = progress[-1] if progress else None
    return StallFacts(
        last_transition=last.name if last else "",
        last_transition_at=last.at if last else "",
        refusing_gate=refusing_gate,
        blocking_labels=tuple(sorted(blocking_labels)),
        unanswered_screen=unanswered_screen(events),
    )
