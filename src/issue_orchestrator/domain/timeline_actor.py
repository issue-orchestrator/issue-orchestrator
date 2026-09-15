"""Which session produced a timeline record keyed to an issue (#6969).

A timeline stream is keyed by issue number, but not every record in it was
produced by that issue's own work. A tech-lead **failure investigation** launches
as an ``issue-{focus}`` session under the focus issue's number while doing
something categorically different: it reads the focus issue as evidence and runs
its own review exchange, branch push and completion inside a disposable scratch
worktree (#6823).

Before this module those records were indistinguishable from implementation
records. The 2026-08-03 health review read issue #6410's board-snapshot timeline
extract, saw a ``review.approved`` and a "Pushed branch to remote" and concluded
the *implementation* was review-approved. It was not: the approval and the push
belonged to the investigation branch. That conclusion was then copied into
recovery guidance which would have merged never-approved branches.

:class:`TimelineActor` is the discriminator that makes the question answerable,
and :func:`classify_timeline_actor` is the only place that answers it.

Determinism and honesty
-----------------------
Classification reads *durable* identity only, in a fixed order:

1. an explicit :data:`TIMELINE_ACTOR_FIELD` stamp written by the producer;
2. the scratch worktree/branch shape owned by
   :mod:`issue_orchestrator.domain.tech_lead_scratch_identity`, recoverable from
   ``run_dir`` / ``worktree_path`` / ``branch_name`` on records already written;
3. otherwise :attr:`TimelineActor.UNKNOWN`.

``UNKNOWN`` is deliberate, not a fallback that guesses ``ISSUE_SESSION``. Records
written before this change that carry no run directory (``agent.coding_completed``
is the notable one) genuinely cannot be attributed, and a reader must not treat an
unattributable record as evidence about the issue's own implementation. Silently
calling them implementation records is exactly the defect this module exists to
stop.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import Enum
from typing import Any

from .tech_lead_scratch_identity import (
    scratch_branch_focus_issue,
    scratch_worktree_focus_issue,
)

logger = logging.getLogger(__name__)

#: Event-data key carrying an explicit, producer-declared actor.
TIMELINE_ACTOR_FIELD = "timeline_actor"


class TimelineActor(str, Enum):
    """Whose session produced a record on an issue's timeline."""

    #: The issue's own coding / rework / review / anchor session.
    ISSUE_SESSION = "issue-session"
    #: A tech-lead failure investigation reading this issue as evidence.
    TECH_LEAD_INVESTIGATION = "tech-lead-investigation"
    #: Not attributable from durable identity. Never evidence about the issue.
    UNKNOWN = "unknown"

    @property
    def is_issue_evidence(self) -> bool:
        """True only when the record describes the issue's own work.

        Consumers that attribute progress, approval or publication to an issue
        must gate on this rather than on the record's issue key.
        """
        return self is TimelineActor.ISSUE_SESSION


def parse_timeline_actor(value: object) -> TimelineActor:
    """Parse a stamped actor value, failing loudly on an unknown vocabulary.

    A typo or a value from a newer writer must not silently degrade into
    ``UNKNOWN`` -- that would hide a real schema break behind the same
    "unattributable" bucket the defect already abuses.
    """
    if isinstance(value, TimelineActor):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{TIMELINE_ACTOR_FIELD} must be a string, got {type(value).__name__}")
    try:
        return TimelineActor(value)
    except ValueError as exc:
        raise ValueError(f"Unknown {TIMELINE_ACTOR_FIELD} value: {value!r}") from exc


def classify_timeline_actor(
    event_data: Mapping[str, Any], *, issue_number: int | None = None
) -> TimelineActor:
    """Classify one timeline record's producing session, for a WRITER.

    ``event_data`` is the record's payload: the live event payload at write time,
    or the stored ``data`` of an already-written record at read time. Both shapes
    resolve identically, so a record classified today keeps the same answer when
    it is re-read tomorrow.

    ``issue_number`` is the stream the record is keyed to. The scratch identity
    embeds the focus issue, so a derivation may only conclude
    "tech-lead-investigation" when that issue matches. Without the check, any
    ANCESTOR directory shaped like a scratch worktree -- a customer's
    ``/srv/customer-tech-lead-7-deadbeefcafe`` checkout, say -- would reclassify
    every ordinary run nested below it and hide real evidence from a health
    review. A shape that matches a DIFFERENT issue is ambiguous, not evidence, so
    it resolves to ``UNKNOWN``.

    This function raises on an unrecognised stamp. That is the right answer for a
    producer or a writer, which must never persist a value the vocabulary does
    not contain; a bulk READER wants :func:`read_timeline_actor` instead.
    """
    stamped = event_data.get(TIMELINE_ACTOR_FIELD)
    if stamped is not None:
        return parse_timeline_actor(stamped)

    # EVERY durable signal is consulted, and any one of them naming an
    # investigation settles it. Reading only the first present signal was a
    # false-negative machine: a validation retry relaunches an investigation on
    # its investigation BRANCH but inside the focus issue's ordinary worktree, so
    # a run-dir-first rule answered "the issue's own work" for a session that was
    # nothing of the sort -- and its `review.approved` and "Pushed branch to
    # remote" went back to being read as the implementation's.
    located = False
    for key in ("run_dir", "worktree_path"):
        value = event_data.get(key)
        if isinstance(value, str) and value:
            located = True
            focus = scratch_worktree_focus_issue(value)
            if focus is not None:
                return _focused(focus, issue_number)

    branch = event_data.get("branch_name")
    if isinstance(branch, str) and branch:
        located = True
        focus = scratch_branch_focus_issue(branch)
        if focus is not None:
            return _focused(focus, issue_number)

    return TimelineActor.ISSUE_SESSION if located else TimelineActor.UNKNOWN


def _focused(focus_issue: int, issue_number: int | None) -> TimelineActor:
    """A scratch shape is evidence only for the issue it names."""
    if issue_number is None or focus_issue == issue_number:
        return TimelineActor.TECH_LEAD_INVESTIGATION
    return TimelineActor.UNKNOWN


def read_timeline_actor(
    event_data: Mapping[str, Any], *, issue_number: int | None = None
) -> TimelineActor:
    """Classify a record being READ back in bulk, never raising.

    A reader aggregates many records that other versions wrote, and one row it
    cannot parse must not be able to take the aggregate down with it. The board
    snapshot is the case that matters: it is a REQUIRED input to every tech-lead
    launch, so a single row carrying an actor from a newer writer would abort
    snapshot construction, exhaust the launch retries, and park the queued
    tech-lead work as needs-human — a denial of service on the very subsystem
    this discriminator exists to protect.

    So the strictness lives where it belongs: a producer/writer still refuses to
    persist an unrecognised value (:func:`classify_timeline_actor`), and a reader
    labels what it cannot recognise ``UNKNOWN`` — never evidence — and says so in
    the log.
    """
    try:
        return classify_timeline_actor(event_data, issue_number=issue_number)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[timeline] unreadable %s on a record for issue %s (%s); treating it"
            " as unattributable",
            TIMELINE_ACTOR_FIELD,
            issue_number,
            exc,
        )
        return TimelineActor.UNKNOWN
