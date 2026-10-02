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
from ..domain.blocked_open_pr import BlockedPRSkipReason
from ..domain.tech_lead_run import WITHDRAWN_SUBJECT_NO_LONGER_ELIGIBLE

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

#: The ``reason=`` of a logged decision that REFUSES planned work: it may not
#: run (a blocking label on its PR or on its issue, in the review lane and the
#: rework lane alike; a queued tech-lead run withdrawn because its subject no
#: longer qualifies), as opposed to waiting its turn. Closed on purpose: the
#: engine's skip reasons are free text across many emitters ("reason=Orchestrator
#: paused", "reason=pending_rework" ...) and nearly all of them are waits, so a
#: reason is a refusal only when its owner says so. A new refusal reason is
#: added here, with its owner's constant.
REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        *(reason.value for reason in BlockedPRSkipReason),
        WITHDRAWN_SUBJECT_NO_LONGER_ELIGIBLE,
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
_QUALIFIED_REFERENCE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
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


def subject_of_text(text: str, *, repo: str | None = None) -> str:
    """The subject a free-text message names, in the event subject's spelling.

    ``PR #N`` for a pull request, ``#N`` for an issue, otherwise
    :data:`ENGINE_SUBJECT`. An ``owner/repo#N`` reference is ``#N`` when it
    names ``repo`` (the engine's own repository) and keeps its full spelling
    for any other repository. The first reference wins: a message is about
    the thing it names first ("Failed to settle ... for issue #4; see #9").
    """
    subjects = subjects_of_text(text, repo=repo)
    return subjects[0] if subjects else ENGINE_SUBJECT


# How the engine logs a decision NOT to do a subject's planned work: a skip,
# drop, refusal or rejection verb, and the decision's ``reason=`` token
# ("[SCANNER] Skipping stale review PR: pr=379 issue=364 reason=issue_blocked",
# "[launch] Dropping stale pending review: ... reason=issue_blocked",
# "[SCANNER] Skipping blocked rework PR: pr=12 issue=4 reason=pr_blocked ...",
# "trace-tech-lead-decision issue=200 ... decision=skip reason=...").
# The opt-in timeline trace repeats a rework skip as "scanner.rework_skip":
# a copy of the line above, not a second refusal, so it is not counted.
_REFUSAL_VERB = re.compile(
    r"\b(?:Skipping|Skipped|Dropping|Dropped|Refusing|[Rr]efused|Rejecting|decision=skip)\b"
)
#: The whole token: a free-text reason ("reason=Orchestrator paused") keeps
#: only its first word, which is never a refusal reason.
_REASON = re.compile(r"\breason=([A-Za-z0-9_.:-]+)")
_REFERENCES: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PR_REFERENCE, "PR #"),
    (_ISSUE_REFERENCE, "#"),
    (_BARE_REFERENCE, "#"),
)


@dataclass(frozen=True)
class WorkRefusal:
    """One logged decision not to do a subject's planned work, for a reason
    that refuses it (:data:`REFUSAL_REASONS`)."""

    #: The subject whose work was refused: the first one the message names.
    subject: str
    #: Every other subject it names (the issue a refused PR review belongs to).
    related: tuple[str, ...]
    reason: str


def subjects_of_text(text: str, *, repo: str | None = None) -> tuple[str, ...]:
    """Every subject ``text`` names, first-named first, in the subject spelling
    of :func:`subject_of_text` (whose answer is the first of these, or
    :data:`ENGINE_SUBJECT` when there is none)."""
    found = [
        (match.start(), match.end(), f"{prefix}{match.group(1)}")
        for pattern, prefix in _REFERENCES
        for match in pattern.finditer(text)
    ]
    for qualified in _QUALIFIED_REFERENCE.finditer(text):
        owner_repo, number = qualified.group(1), qualified.group(2)
        own = repo is not None and owner_repo.casefold() == repo.casefold()
        found.append((qualified.start(), qualified.end(), f"#{number}" if own else f"{owner_repo}#{number}"))
    subjects: list[str] = []
    end = -1
    # Longest first at one position; a reference inside an earlier one ("#12"
    # of "PR #12") is part of it, not a second subject.
    for start, stop, subject in sorted(found, key=lambda f: (f[0], -f[1])):
        if start >= end:
            subjects.append(subject)
            end = stop
    return tuple(dict.fromkeys(subjects))


def refusal_of_text(text: str, *, repo: str | None = None) -> WorkRefusal | None:
    """The work refusal ``text`` logs, or None.

    A refusal is a skip/drop/refuse/reject decision whose ``reason=`` is a
    refusal reason (:data:`REFUSAL_REASONS`), about a subject the message
    names. Its level does not matter: the engine logs these at INFO, because
    one of them is routine; it is their REPEATING for a subject that nothing
    moves that is the livelock (a PR's review queued and dropped on every scan).
    """
    if _REFUSAL_VERB.search(text) is None or (reason := _REASON.search(text)) is None:
        return None
    if reason.group(1) not in REFUSAL_REASONS:
        return None
    subjects = subjects_of_text(text, repo=repo)
    if not subjects:
        return None
    return WorkRefusal(subject=subjects[0], related=subjects[1:], reason=reason.group(1))


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


def subjects_changed_by(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Every subject a state-change event changes, in the subject spelling.

    An event about an issue's pull request (``pr.view_changed`` carries both
    numbers) changes the issue AND the PR, so a failure logged against either
    one is no longer a repeat after it. Empty for an event that changes no
    subject's state.
    """
    if not resets_subject(event):
        return ()
    subjects = [_subject(event)]
    pr_number = _payload(event).get("pr_number")
    if isinstance(pr_number, (int, str)) and not isinstance(pr_number, bool) and str(pr_number):
        subjects.append(f"PR #{pr_number}")
    return tuple(dict.fromkeys(subjects))


def resets_subject(event: Mapping[str, Any]) -> bool:
    """Whether ``event`` changes its subject's state (so its failures are not repeats)."""
    return str(event.get("type", "")) in STATE_CHANGES


def find_repeating_failures(
    events: Iterable[Mapping[str, Any]], *, threshold: int = LIVELOCK_THRESHOLD
) -> tuple[RepeatingFailure, ...]:
    """Every failure signature that repeated ``threshold``+ times in a row of
    its subject's history with no state change in between (the peak run).

    The exam's question: did the engine livelock at any point in its run?"""
    _running, peak = _runs(events, threshold)
    return _over(peak, threshold)


def find_current_repeats(
    events: Iterable[Mapping[str, Any]], *, threshold: int = LIVELOCK_THRESHOLD
) -> tuple[RepeatingFailure, ...]:
    """Every failure signature whose run at the END of ``events`` is ``threshold``+.

    The audit's question: is the engine livelocked now? A run a later state
    change ended has been left behind, however long it was.
    """
    running, _peak = _runs(events, threshold)
    return _over(running, threshold)


def _runs(
    events: Iterable[Mapping[str, Any]], threshold: int
) -> tuple[Counter[tuple[str, str, str]], Counter[tuple[str, str, str]]]:
    """The running and peak repeat counts of each failure signature."""
    if threshold < 2:
        raise ValueError("a livelock needs a threshold of at least 2 repeats")
    running: Counter[tuple[str, str, str]] = Counter()
    peak: Counter[tuple[str, str, str]] = Counter()
    for event in events:
        name = str(event.get("type", ""))
        subject = _subject(event)
        if name in STATE_CHANGES:
            changed = subjects_changed_by(event)
            for key in [key for key in running if key[1] in changed]:
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
    return running, peak


def _over(counts: Counter[tuple[str, str, str]], threshold: int) -> tuple[RepeatingFailure, ...]:
    return tuple(
        RepeatingFailure(event=name, subject=subject, detail=detail, count=count)
        for (name, subject, detail), count in sorted(counts.items())
        if count >= threshold
    )
