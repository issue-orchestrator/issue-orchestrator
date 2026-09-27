"""The engine repeating the same failure for the same subject, getting nowhere.

The #7345/#7346 class: a settlement or a drain fails identically on every
tick, forever, with no terminal state — and nothing on the board changes.
The engine is busy (so the drive loop never sees it quiet) while nothing
progresses. This finds it on the engine's own event stream:

* a *failure signature* is a failure-class event type, its subject (issue,
  PR, or the engine itself for board-wide work), and its salient detail
  (``step_type`` / ``reason`` / ``failure_reason`` / normalized ``error``);
* a *state change* for the subject — its labels, its PR, a session, or a
  successful application of the same step — resets that subject's counts;
* a signature repeating ``threshold`` or more times without one is a livelock.

The threshold sits above the engine's own bounded retries (three attempts,
e.g. the review-exchange no-completion budget) and far below a per-tick
loop (porchpin#410 repeated its reconcile pause 130 times).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

LIVELOCK_THRESHOLD = 5

#: Events that report something failing, refused or stuck.
FAILURE_EVENTS: frozenset[str] = frozenset(
    {
        "apply.failed",
        "reconciliation.required",
        "issue.paused_reconcile",
        "stale.in_progress_detected",
        "tech_lead.run_held",
        "tech_lead.decision_rejected",
        "review.skipped",
        "rework.skipped",
        "publish.failed",
        "validated_work.capture_failed",
        "session.failed",
        "session.start_failed",
        "review_exchange.role_timeout",
        "merge_queue.failed",
    }
)

#: Events that change a subject's state; any of them resets its counts.
STATE_CHANGES: frozenset[str] = frozenset(
    {
        "issue.labels_changed",
        "pr.view_changed",
        "session.started",
        "session.launched",
        "session.completed",
        "review.started",
        "review.approved",
        "review.changes_requested",
    }
)

_DETAIL_KEYS = ("step_type", "reason", "failure_reason", "failure")
_VOLATILE = re.compile(r"\d+")


@dataclass(frozen=True)
class RepeatingFailure:
    event: str
    subject: str
    detail: str
    count: int

    def describe(self) -> str:
        detail = f" [{self.detail}]" if self.detail else ""
        return (
            f"{self.event}{detail} on {self.subject} repeated {self.count}x"
            " with no state change"
        )

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "subject": self.subject, "detail": self.detail, "count": self.count}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RepeatingFailure":
        return cls(
            event=str(data["event"]),
            subject=str(data["subject"]),
            detail=str(data["detail"]),
            count=int(data["count"]),
        )


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _subject(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    for value, prefix in (
        (event.get("issue_key"), "#"),
        (payload.get("issue_number"), "#"),
        (payload.get("pr_number"), "PR #"),
    ):
        if isinstance(value, (int, str)) and not isinstance(value, bool) and str(value):
            return f"{prefix}{value}"
    return "the engine"


def _detail(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    parts = [str(payload[key]) for key in _DETAIL_KEYS if payload.get(key)]
    error = payload.get("error")
    if isinstance(error, str) and error:
        # The same failure carries changing ids/counts; the shape repeats.
        parts.append(_VOLATILE.sub("N", error)[:160])
    return "; ".join(parts)


def find_repeating_failures(
    events: Iterable[Mapping[str, Any]], *, threshold: int = LIVELOCK_THRESHOLD
) -> tuple[RepeatingFailure, ...]:
    """Every failure signature that repeated ``threshold``+ times in a row of
    its subject's history with no state change in between (the peak run)."""
    if threshold < 2:
        raise ValueError("a livelock needs a threshold of at least 2 repeats")
    running: Counter[tuple[str, str, str]] = Counter()
    peak: Counter[tuple[str, str, str]] = Counter()
    for event in events:
        name = str(event.get("type", ""))
        subject = _subject(event)
        if name in STATE_CHANGES:
            for key in [key for key in running if key[1] == subject]:
                del running[key]
        elif name == "apply.step_applied" and _payload(event).get("result") == "success":
            step = str(_payload(event).get("step_type", ""))
            for key in [key for key in running if key[1] == subject and key[2].startswith(step)]:
                del running[key]
        elif name in FAILURE_EVENTS:
            key = (name, subject, _detail(event))
            running[key] += 1
            peak[key] = max(peak[key], running[key])
    return tuple(
        RepeatingFailure(event=name, subject=subject, detail=detail, count=count)
        for (name, subject, detail), count in sorted(peak.items())
        if count >= threshold
    )
