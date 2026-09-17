"""Which branch a timeline event is about (#7263, #7268).

Three producers name a branch on a timeline event: ``review.started`` and
``review.approved``/``review.changes_requested`` from
:mod:`issue_orchestrator.control.completion_processor`, and
``session.processing_completed`` from
:mod:`issue_orchestrator.control.session_controller`. Each one used to decide
for itself when to read the checkout and when to omit the field, and the two
copies of that policy had already drifted: a cached review replay re-read the
CURRENT branch, so after PR-collision remediation renamed it the event named a
branch the review had never seen.

:class:`BranchSubject` is the owner. It has exactly two sources -- a branch
RETAINED with the artifact the event replays, and a branch SAMPLED from the
checkout -- and one rendering. Producers ask it for the subject and spread the
fields; they hold no policy of their own.

Retained beats sampled. The retained value was fixed when the work it describes
happened and cannot drift afterwards; the checkout is mutable and answers only
for right now. Sampling is correct exactly when the event IS about right now,
which is the fresh-exchange and processing-completed case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict


class BranchEventFields(TypedDict):
    """What a branch subject contributes to an event payload.

    Typed rather than ``dict[str, str]`` so producers whose payloads are
    TypedDicts can spread it without a cast, and so the field NAME stays this
    module's to choose.
    """

    branch_name: NotRequired[str]


class CurrentBranchReader(Protocol):
    """The one thing this module needs from a working copy.

    ``get_current_branch`` contracts to return ``None`` -- not to raise -- on a
    detached HEAD or an unreadable checkout, which is why nothing here carries a
    defensive ``except``. Every implementation in the tree is held to that by
    ``tests/unit/ports/test_working_copy_branch_contract.py``.
    """

    def get_current_branch(self, worktree: Path) -> str | None: ...


class RetainedBranchFacts(Protocol):
    """An artifact that recorded the branch its work happened on."""

    @property
    def branch_name(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class BranchSubject:
    """The branch an event is about, or nothing when it cannot be named."""

    branch_name: str | None

    @classmethod
    def unknown(cls) -> "BranchSubject":
        """No branch can be named; the event carries no branch field."""
        return cls(branch_name=None)

    @classmethod
    def named(cls, branch_name: str | None) -> "BranchSubject":
        """A branch already in hand. Blank is the same as absent."""
        if branch_name is None or not branch_name.strip():
            return cls(branch_name=None)
        return cls(branch_name=branch_name.strip())

    @classmethod
    def sampled(
        cls, reader: CurrentBranchReader | None, worktree: Path | None
    ) -> "BranchSubject":
        """Read the checkout. For events that are about its CURRENT state."""
        if reader is None or worktree is None:
            return cls.unknown()
        return cls.named(reader.get_current_branch(worktree))

    @classmethod
    def retained(cls, facts: RetainedBranchFacts | None) -> "BranchSubject":
        """Replay the branch an artifact recorded when its work happened."""
        if facts is None:
            return cls.unknown()
        return cls.named(facts.branch_name)

    @classmethod
    def reviewed(
        cls,
        facts: RetainedBranchFacts | None,
        reader: CurrentBranchReader | None,
        worktree: Path | None,
    ) -> "BranchSubject":
        """The branch a REPLAYED review covered.

        The whole retained-versus-sampled rule, decided here rather than by the
        caller: replay what the artifact recorded, and read the checkout only
        when it recorded nothing. A pre-retention summary is the sole remaining
        case where a replay can name a renamed branch, and losing the field
        entirely there was measured to be worse than the rare inaccuracy
        (#7269 review F2).

        The checkout is read ONLY in that fallback. An owner that always reads
        it and then discards the answer is not an owner of the decision, it is a
        renderer of one the caller already made -- and it pays for a git call on
        every cache hit (#7269 round 3, finding [2]).
        """
        retained = cls.retained(facts)
        if retained.branch_name is not None:
            return retained
        return cls.sampled(reader, worktree)

    def as_event_fields(self) -> BranchEventFields:
        """Render as event-payload fields; empty when no branch is known.

        Absent, not null: consumers distinguish "this event names no branch"
        from "this event names the branch None".
        """
        if self.branch_name is None:
            return {}
        return {"branch_name": self.branch_name}
