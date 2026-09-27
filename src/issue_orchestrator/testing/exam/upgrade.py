"""Grading an upgrade: restarting onto new code over the old code's state.

Case U stops an engine at a base ref with work in flight (no drain) and
starts the candidate ref from the SAME checkout and state directory. Agent
sessions do not survive an engine stop — every agent is a PTY child of the
engine — so what the candidate inherits is state: the run ledger, pending-work
claims, GitHub labels and every sqlite store's schema. The upgrade is sound
when the candidate takes all of that over quietly:

* nothing is quarantined or declared unrestorable (the restore owners'
  ``session.*`` hazard events);
* in the first ``early_ticks`` ticks it posts no comment and adds no hold
  label (needs-human, blocked-*), the writes an operator would be paged by;
* and, graded by the case's goals, the in-flight work then completes.

Every other GitHub write in the window is reported by kind, not failed: a
restart may legitimately re-sync a label.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

#: Events the restore path publishes when it cannot take a run over
#: (``events/catalog.py``): each one means a needs-human escalation.
HAZARD_EVENTS: frozenset[str] = frozenset(
    {
        "session.run_unrestorable",
        "session.claim_unreadable",
        "session.run_unrestorable_claim_unreadable",
    }
)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class WriteKind(str, Enum):
    """What a GitHub write did, read from its ``gh_audit`` command key."""

    LABEL_ADD = "label_add"
    LABEL_REMOVE = "label_remove"
    COMMENT = "comment"
    ISSUE_EDIT = "issue_edit"
    PULL_REQUEST = "pull_request"
    GRAPHQL = "graphql"
    """GraphQL is POSTed whether it reads or writes; reported, never graded."""
    OTHER = "other"


def classify_write(command: str) -> WriteKind | None:
    """The kind of write a ``"<METHOD> <path>"`` audit key is; ``None`` for a read."""
    method, _, path = command.strip().partition(" ")
    method = method.upper()
    if method not in _WRITE_METHODS:
        return None
    if path == "/graphql":
        return WriteKind.GRAPHQL
    parts = path.strip("/").split("/")
    # /repos/{owner}/{repo}/<resource>/...
    resource = parts[3:] if len(parts) >= 4 and parts[0] == "repos" else []
    if resource[:1] == ["issues"] and len(resource) >= 3:
        if resource[2] == "labels":
            return WriteKind.LABEL_REMOVE if method == "DELETE" else WriteKind.LABEL_ADD
        if resource[2] == "comments":
            return WriteKind.COMMENT
    if resource[:1] == ["issues"] and len(resource) == 2:
        return WriteKind.ISSUE_EDIT
    if resource[:1] == ["pulls"]:
        return WriteKind.PULL_REQUEST
    return WriteKind.OTHER


def writes_by_kind(by_command: Mapping[str, int]) -> dict[WriteKind, int]:
    """Writes per kind from a ``gh_audit`` ``by_command`` mapping (reads skipped)."""
    counts = {kind: 0 for kind in WriteKind}
    for command, calls in by_command.items():
        kind = classify_write(command)
        if kind is not None:
            counts[kind] += calls
    return counts


@dataclass(frozen=True)
class LabelChange:
    """One ``issue.labels_changed`` event the engine published."""

    issue_number: int
    added: tuple[str, ...]
    removed: tuple[str, ...]

    def describe(self) -> str:
        parts = [f"+{label}" for label in self.added] + [f"-{label}" for label in self.removed]
        return f"#{self.issue_number} {' '.join(parts)}"

    def to_dict(self) -> dict[str, Any]:
        return {"issue_number": self.issue_number, "added": list(self.added), "removed": list(self.removed)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LabelChange":
        return cls(
            issue_number=int(data["issue_number"]),
            added=tuple(str(label) for label in data["added"]),
            removed=tuple(str(label) for label in data["removed"]),
        )


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def label_changes(events: Iterable[Mapping[str, Any]]) -> tuple[LabelChange, ...]:
    """Every label change in ``events``, in order."""
    return tuple(
        LabelChange(
            issue_number=int(payload["issue_number"]),
            added=tuple(str(label) for label in payload.get("added", ())),
            removed=tuple(str(label) for label in payload.get("removed", ())),
        )
        for event in events
        if event.get("type") == "issue.labels_changed"
        for payload in (_payload(event),)
    )


def complete_history(events: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
    """``events`` (an engine's buffered history from id 0), checked complete.

    A restart's hazards are published during startup, before any watcher has
    connected, so they are read from the engine's replay buffer. A buffer
    that has already dropped its oldest events cannot prove there were none.
    """
    ids = [event.get("event_id") for event in events]
    if not events or ids[0] != 1 or ids != list(range(1, len(ids) + 1)):
        head = ids[:3]
        raise ValueError(
            f"engine event history is incomplete (first ids {head} of {len(ids)});"
            " a restart window cannot be graded from a truncated buffer"
        )
    return events


def merged_by_id(*streams: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One ordered stream from overlapping ones (the replayed history and the
    watcher's live stream), each event once."""
    seen: dict[int, Mapping[str, Any]] = {}
    for stream in streams:
        for event in stream:
            event_id = event.get("event_id")
            if not isinstance(event_id, int) or isinstance(event_id, bool):
                raise ValueError(f"engine event without an integer event_id: {event!r}")
            seen.setdefault(event_id, event)
    return [seen[event_id] for event_id in sorted(seen)]


def hazard_events(events: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every restore-hazard event in ``events``, described for the report."""
    return tuple(
        f"{event.get('type')} on #{payload.get('issue_number')}"
        f" ({payload.get('cause') or payload.get('error') or 'no cause given'})"
        for event in events
        if event.get("type") in HAZARD_EVENTS
        for payload in (_payload(event),)
    )


@dataclass(frozen=True)
class UpgradeSpec:
    """What a sound upgrade looks like for one case."""

    early_ticks: int
    """How many candidate ticks the quiet window covers."""
    hold_labels: frozenset[str]
    """Labels the candidate must not add in the window (needs-human, blocks)."""

    def __post_init__(self) -> None:
        if self.early_ticks < 1:
            raise ValueError("an upgrade window needs at least one tick")
        if not self.hold_labels:
            raise ValueError("an upgrade spec must name the hold labels it forbids")


@dataclass(frozen=True)
class UpgradeFacts:
    """What the harness saw across the stop and the restart."""

    base_commit: str
    candidate_commit: str
    sessions_at_stop: tuple[int, ...]
    """Issues with a live session when the base engine was stopped."""
    early_ticks: int
    """Candidate ticks completed when the window was measured."""
    early_writes: Mapping[WriteKind, int]
    early_label_changes: tuple[LabelChange, ...]
    hazards: tuple[str, ...]
    """Restore-hazard events over the WHOLE candidate run, not only the window."""
    startup_error: str = ""
    """Why the candidate did not start, when it did not."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "sessions_at_stop": list(self.sessions_at_stop),
            "early_ticks": self.early_ticks,
            "early_writes": {kind.value: self.early_writes.get(kind, 0) for kind in WriteKind},
            "early_label_changes": [change.to_dict() for change in self.early_label_changes],
            "hazards": list(self.hazards),
            "startup_error": self.startup_error,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "UpgradeFacts":
        writes = data["early_writes"]
        return cls(
            base_commit=str(data["base_commit"]),
            candidate_commit=str(data["candidate_commit"]),
            sessions_at_stop=tuple(int(n) for n in data["sessions_at_stop"]),
            early_ticks=int(data["early_ticks"]),
            early_writes={kind: int(writes[kind.value]) for kind in WriteKind},
            early_label_changes=tuple(LabelChange.from_dict(c) for c in data["early_label_changes"]),
            hazards=tuple(str(h) for h in data["hazards"]),
            startup_error=str(data["startup_error"]),
        )


@dataclass(frozen=True)
class UpgradeGrade:
    facts: UpgradeFacts
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": list(self.failures), "facts": self.facts.to_dict()}


def grade_upgrade(spec: UpgradeSpec, facts: UpgradeFacts) -> UpgradeGrade:
    failures: list[str] = []
    if facts.startup_error:
        failures.append(f"candidate did not start: {facts.startup_error}")
    elif facts.early_ticks < spec.early_ticks:
        failures.append(
            f"candidate completed only {facts.early_ticks} of {spec.early_ticks} ticks"
            " in the restart window"
        )
    failures.extend(f"restore hazard: {hazard}" for hazard in facts.hazards)
    comments = facts.early_writes.get(WriteKind.COMMENT, 0)
    if comments:
        failures.append(f"{comments} comment(s) posted in the first {spec.early_ticks} ticks")
    failures.extend(
        f"hold label added in the first {spec.early_ticks} ticks: {change.describe()}"
        for change in facts.early_label_changes
        if set(change.added) & spec.hold_labels
    )
    return UpgradeGrade(facts=facts, failures=tuple(failures))
