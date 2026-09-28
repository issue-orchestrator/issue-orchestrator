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

One signature method serves every reader of it: the tech-lead exam grades a
run on its event stream, and ``io engine-audit`` (#7490) reads the same
events from an engine's timeline and the same shapes from its log, so both
normalize a message with :func:`normalize_signature` and name a subject with
:func:`subject_of_text` / the event subject rule here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..control.session_launch_types import REVIEW_HELD_BY_RECOVERY

LIVELOCK_THRESHOLD = 5

#: Events that report an action failing or being refused. Pure observations
#: (``stale.in_progress_detected``: the engine noticing a claim that may
#: legitimately stay paused) are not failures, however often they repeat.
FAILURE_EVENTS: frozenset[str] = frozenset(
    {
        "apply.failed",
        "reconciliation.required",
        "issue.paused_reconcile",
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

#: Skip reasons that mean WAITING, not failing: capacity and an operator
#: pause (``control/workflows/review_workflow.py``,
#: ``retrospective_review_workflow.py``). A skip for any other reason (e.g.
#: ``stale_pending_review:issue_blocked``) is a refusal and counts.
_WAITING_SKIP_REASONS: frozenset[str] = frozenset(
    {
        "no_capacity",
        "orchestrator_paused",
        "retrospective_review_no_capacity",
        "retrospective_review_orchestrator_paused",
        # A queued review waiting for the recovery owner to release its issue
        # (#7455); the owner routes and releases it, so it is a wait.
        REVIEW_HELD_BY_RECOVERY,
    }
)
_SKIP_EVENTS = frozenset({"review.skipped", "rework.skipped"})

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

#: The subject of a failure that names no issue or PR: board-wide work.
ENGINE_SUBJECT = "the engine"

#: How much of a normalized message a signature keeps.
SIGNATURE_LENGTH = 160

# Volatile parts of one failure's message, most specific first: the same
# failure carries a fresh timestamp, id, SHA or count every time it repeats,
# and only its shape repeats.
_ISO_INSTANT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}\b"
)
#: A hex run long enough to be a SHA or an opaque id, with a letter in it (a
#: run of digits alone is a number, normalized below).
_HEX_ID = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]*\d[0-9a-fA-F]*\b")
_NUMBER = re.compile(r"\d+")

# How a log message names its subject, most specific first. Issue references
# come as "issue #410", "issue=410", "issue-410" (a session name), "issue 410"
# or a bare "#410"; pull requests as "PR #12" / "pr=12".
_PR_REFERENCE = re.compile(r"\b(?:PR|pr|pull request)[ =#-]*#?(\d+)\b")
_ISSUE_REFERENCE = re.compile(r"\b[Ii]ssue[ =#:-]*#?(\d+)\b")
_BARE_REFERENCE = re.compile(r"(?<![\w/#&])#(\d+)\b")


def normalize_signature(text: str) -> str:
    """The repeatable shape of one failure message.

    Strips what changes between two occurrences of the same failure:
    timestamps, UUIDs, SHAs and hex ids, then every remaining number. Two
    messages with the same shape normalize to the same signature.
    """
    shape = _ISO_INSTANT.sub("<time>", text)
    shape = _UUID.sub("<id>", shape)
    shape = _HEX_ID.sub(lambda m: "<sha>" if len(m.group()) >= 7 else m.group(), shape)
    return _NUMBER.sub("N", shape)[:SIGNATURE_LENGTH]


def subject_of_text(text: str) -> str:
    """The subject a free-text message names, in the event subject's spelling.

    ``PR #N`` for a pull request, ``#N`` for an issue, otherwise
    :data:`ENGINE_SUBJECT`. The first reference wins: a message is about the
    thing it names first ("Failed to settle ... for issue #4; see #9").
    """
    found = [
        (match.start(), f"{prefix}{match.group(1)}")
        for pattern, prefix in (
            (_PR_REFERENCE, "PR #"),
            (_ISSUE_REFERENCE, "#"),
            (_BARE_REFERENCE, "#"),
        )
        for match in [pattern.search(text)]
        if match is not None
    ]
    return min(found)[1] if found else ENGINE_SUBJECT


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
    return ENGINE_SUBJECT


def _detail(event: Mapping[str, Any]) -> str:
    payload = _payload(event)
    parts = [str(payload[key]) for key in _DETAIL_KEYS if payload.get(key)]
    error = payload.get("error")
    if isinstance(error, str) and error:
        # The same failure carries changing ids/counts; the shape repeats.
        parts.append(normalize_signature(error))
    return "; ".join(parts)


def subject_of_event(event: Mapping[str, Any]) -> str:
    """The subject an engine event is about (see the module docstring)."""
    return _subject(event)


def resets_subject(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` changes its subject's state (so its failures are not repeats)."""
    return str(event.get("type", "")) in STATE_CHANGES


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
            if name in _SKIP_EVENTS and _payload(event).get("reason") in _WAITING_SKIP_REASONS:
                continue
            key = (name, subject, _detail(event))
            running[key] += 1
            peak[key] = max(peak[key], running[key])
    return tuple(
        RepeatingFailure(event=name, subject=subject, detail=detail, count=count)
        for (name, subject, detail), count in sorted(peak.items())
        if count >= threshold
    )
