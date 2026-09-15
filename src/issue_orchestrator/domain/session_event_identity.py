"""The identity fields every session-keyed timeline event carries (#6969).

Five separate payload builders in ``control.completion_handler`` and several more
in ``control.session_launcher`` each hand-assembled the same identity dict out of
``Session`` internals -- ``session.issue.number``, ``session.terminal_id``,
``session.agent_label``, ``session.key.task.value``, ``session.rework_cycle``.
Adding one more identity field to that shape meant editing every site and hoping
none was missed, which is precisely how the #6969 discriminator went missing in
the first place: there was no owner to add it to.

:class:`SessionEventIdentity` is that owner. It reads a session once, decides the
full identity -- including which actor the resulting timeline records belong to
(:mod:`issue_orchestrator.domain.timeline_actor`) -- and renders it as event
fields. Producers spread it into their payload and add only what is specific to
their event.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .session_key import SessionKey
from .tech_lead_scratch_identity import (
    is_scratch_branch_name,
    path_is_under_scratch_worktree,
)
from .timeline_actor import TIMELINE_ACTOR_FIELD, TimelineActor


class _SubjectIssue(Protocol):
    @property
    def number(self) -> int: ...


class SessionIdentityFacts(Protocol):
    """The subset of a running session that fixes its timeline identity.

    A Protocol rather than ``Session`` itself: this module is domain-level and
    ``domain.models`` already imports widely, so depending on the shape keeps the
    dependency one-way and lets restored/partial session views be classified too.
    Members are read-only properties so an implementer's more specific attribute
    types (``Session.issue`` is an ``IssueProtocol``) stay compatible.
    """

    @property
    def key(self) -> SessionKey: ...
    @property
    def issue(self) -> _SubjectIssue: ...
    @property
    def terminal_id(self) -> str: ...
    @property
    def agent_label(self) -> str | None: ...
    @property
    def rework_cycle(self) -> int | None: ...
    @property
    def worktree_path(self) -> Path: ...
    @property
    def branch_name(self) -> str: ...


def timeline_actor_for_session(session: SessionIdentityFacts) -> TimelineActor:
    """Which actor a session's timeline records belong to.

    A tech-lead failure investigation is launched with a disposable scratch
    worktree (``scratch_worktree``); that flag is the producer's own declaration
    and wins. A session RESTORED across an orchestrator restart is rebuilt without
    it, so the scratch worktree path and branch -- both durable, both owned by
    :mod:`issue_orchestrator.domain.tech_lead_scratch_identity` -- answer for it.

    Every other session, tech-lead batch and health reviews included, works on the
    issue its records are keyed to, so its records ARE that issue's evidence.
    """
    # getattr, like control.tech_lead_termination, because a session view rebuilt
    # after a restart carries the worktree and branch but not the launch flag.
    if getattr(session, "scratch_worktree", False):
        return TimelineActor.TECH_LEAD_INVESTIGATION
    if path_is_under_scratch_worktree(str(session.worktree_path)):
        return TimelineActor.TECH_LEAD_INVESTIGATION
    if is_scratch_branch_name(session.branch_name):
        return TimelineActor.TECH_LEAD_INVESTIGATION
    return TimelineActor.ISSUE_SESSION


@dataclass(frozen=True)
class SessionEventIdentity:
    """Who a session-keyed timeline event is about, and who produced it."""

    issue_number: int
    session_id: str
    agent: str | None
    task: str
    rework_cycle: int | None
    timeline_actor: TimelineActor

    @classmethod
    def of(cls, session: SessionIdentityFacts) -> "SessionEventIdentity":
        """Read a live session's identity once."""
        return cls(
            issue_number=session.issue.number,
            session_id=session.terminal_id,
            agent=session.agent_label,
            task=session.key.task.value,
            rework_cycle=session.rework_cycle,
            timeline_actor=timeline_actor_for_session(session),
        )

    def as_event_fields(self) -> dict[str, Any]:
        """Render the identity as event-payload fields."""
        return {
            "issue_number": self.issue_number,
            "session_id": self.session_id,
            "agent": self.agent,
            "task": self.task,
            "rework_cycle": self.rework_cycle,
            TIMELINE_ACTOR_FIELD: self.timeline_actor.value,
        }
