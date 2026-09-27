"""Action liveness: one policy for how often a replanned action may fail (#7350).

Every loop the 2026-09-27 census found had the same shape. A desired action is
re-derived each pass from durable facts that its own failure never changes, and
the failure is written nowhere the deriver reads. So the next pass plans the
identical action, it fails identically, and nothing ever counts. Each site
decided "transient or permanent?" for itself, and nothing bounded the repeats.

This module is the vocabulary and the arithmetic of the one answer:

* every attempted action reports an :class:`ActionOutcome` -
  ``done | transient(retry_at) | permanent(reason) | needs_human(reason)``;
* attempts are keyed on a :class:`LivenessKey`: the subject, the action, and a
  FINGERPRINT of the facts the action was derived from. "Nothing changed" means
  the same fingerprint, so a changed fact is a new question with a fresh budget,
  while the same question asked again spends from the old one;
* :class:`LivenessPolicy` turns a failed outcome into the next durable
  :class:`LivenessRow`: a backoff for a transient failure, and PARKED once the
  budget is spent or the failure is permanent or needs a human.

A parked row is terminal for its fingerprint. The planner, the recovery drain
and every other replanning path consult it before they try again, and it is
released only by progress: the facts change, the action succeeds under any
fingerprint, or an operator retries the subject.

Pure: no clock, no storage, no I/O. The control-layer owner supplies ``now``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from pathlib import PurePath

from .host_rate_limit import RATE_LIMIT_DEFERRAL_BOUND
from .owed_write import NO_DEBT, EffectDebt, EffectResult


class OutcomeKind(StrEnum):
    """How one attempt of an action ended, as the liveness owner classifies it."""

    #: The action did what it was for, or found there was nothing to do.
    DONE = "done"
    #: It failed in a way time may heal. Retried after a backoff, a bounded
    #: number of times with the same facts.
    TRANSIENT = "transient"
    #: It failed in a way repetition cannot heal: the same facts will fail the
    #: same way. Parked on the first occurrence.
    PERMANENT = "permanent"
    #: It cannot proceed until a person acts. Parked on the first occurrence.
    NEEDS_HUMAN = "needs_human"
    #: It cannot run yet because another owner holds a legitimate precondition
    #: (an issue's runtime is active). Not a failure: it spends nothing and
    #: never parks, but it is paced at ``max_backoff`` and stays visible.
    WAITING = "waiting"


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """One attempt's typed result.

    ``retry_at`` is only for a transient failure whose source said when it will
    answer again (a GitHub rate limit's reset, for one). Such a wait spends no
    attempt, because nothing about the work failed, but only for
    :attr:`LivenessPolicy.declared_wait_bound`: a limit that never lifts must
    still reach a person.
    """

    kind: OutcomeKind
    reason: str = ""
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind is OutcomeKind.DONE:
            if self.reason or self.retry_at is not None:
                raise ValueError("a done outcome carries no reason and no retry_at")
            return
        if not self.reason.strip():
            raise ValueError(f"a {self.kind.value} outcome requires a reason")
        if self.retry_at is not None:
            if self.kind is not OutcomeKind.TRANSIENT:
                raise ValueError("only a transient outcome may declare retry_at")
            _require_aware(self.retry_at, "retry_at")

    @classmethod
    def done(cls) -> "ActionOutcome":
        return cls(OutcomeKind.DONE)

    @classmethod
    def transient(cls, reason: str, retry_at: datetime | None = None) -> "ActionOutcome":
        return cls(OutcomeKind.TRANSIENT, reason, retry_at)

    @classmethod
    def permanent(cls, reason: str) -> "ActionOutcome":
        return cls(OutcomeKind.PERMANENT, reason)

    @classmethod
    def needs_human(cls, reason: str) -> "ActionOutcome":
        return cls(OutcomeKind.NEEDS_HUMAN, reason)

    @classmethod
    def waiting(cls, reason: str) -> "ActionOutcome":
        return cls(OutcomeKind.WAITING, reason)


@dataclass(frozen=True, slots=True)
class ActionIdentity:
    """WHICH action on WHICH subject, independent of the facts behind it.

    ``subject`` is namespaced (``issue:410``, ``validated_work:r1:...``) so two
    stores' ids can never collide; ``action`` names the operation. Success under
    any fingerprint clears every row of the identity: the action made progress.
    """

    subject: str
    action: str

    def __post_init__(self) -> None:
        if not self.subject.strip() or not self.action.strip():
            raise ValueError("an action identity requires a subject and an action")


@dataclass(frozen=True, slots=True)
class LivenessKey:
    """An identity plus the fingerprint of the facts one attempt acted on.

    ``escalation_issue`` is the issue a person is shown when this key parks:
    the needs-human block and its comment go there. ``None`` when the subject
    has no issue to carry it; the timeline event and the tech-lead board still
    show the park.
    """

    identity: ActionIdentity
    fingerprint: str
    escalation_issue: int | None = None

    def __post_init__(self) -> None:
        if not self.fingerprint:
            raise ValueError("a liveness key requires a fact fingerprint")
        if self.escalation_issue is not None and self.escalation_issue <= 0:
            raise ValueError("escalation_issue must be a positive issue number")


def fact_fingerprint(facts: object) -> str:
    """A stable digest of the facts an action was derived from.

    Stable across processes: sets are sorted and dataclasses are walked field by
    field, so ``PYTHONHASHSEED`` cannot change it and a restart does not reset
    every budget. Anything this cannot canonicalize is a programming error and
    raises, rather than silently hashing an address that differs every tick.
    """
    encoded = json.dumps(_canonical(facts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]


def _canonical(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return _canonical(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    liveness_facts = getattr(value, "liveness_facts", None)
    if callable(liveness_facts):
        # A nested action (a wrapper's effect) contributes its own facts.
        return _canonical(liveness_facts())
    if is_dataclass(value) and not isinstance(value, type):
        # A field named ``*_at`` records WHEN something was sampled or decided
        # (``assessed_at``, ``decided_at``, ``observed_at``), not what: fresh on
        # every plan while the facts stand still, it would give each failure a
        # new fingerprint and a fresh budget.
        return {
            item.name: _canonical(getattr(value, item.name))
            for item in fields(value)
            if not item.name.endswith("_at")
        }
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    raise TypeError(f"cannot fingerprint a {type(value).__name__}: {value!r}")


@dataclass(frozen=True, slots=True)
class LivenessRow:
    """The durable failure history of one key. Absent means "never failed".

    ``attempts`` counts failed attempts that SPENT budget, so a declared wait
    does not appear in it. ``next_attempt_at`` is ``None`` exactly when parked.
    ``escalated`` records that the human-visible block for this park committed,
    and ``explained`` that its explanatory comment did. Until both have,
    ``escalation`` paces the owner's retries of the one still owed
    (:meth:`LivenessPolicy.effect_due`); it restarts when the block lands and
    the comment becomes the debt.
    """

    key: LivenessKey
    attempts: int
    first_failed_at: datetime
    last_failed_at: datetime
    last_outcome: OutcomeKind
    last_reason: str
    next_attempt_at: datetime | None
    escalated: bool = False
    explained: bool = False
    escalation: EffectDebt = NO_DEBT
    #: When a replanning path last asked this exact question (admitted or
    #: held). ``None`` until it is asked again after its own attempt. A row
    #: nobody has asked about for :attr:`LivenessPolicy.stale_after` is no
    #: longer a question, and the owner retires it.
    last_planned_at: datetime | None = None

    @property
    def planned_at(self) -> datetime:
        return self.last_planned_at or self.last_failed_at

    def __post_init__(self) -> None:
        if self.last_outcome is OutcomeKind.DONE:
            raise ValueError("a liveness row records failures only")
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        _require_aware(self.first_failed_at, "first_failed_at")
        _require_aware(self.last_failed_at, "last_failed_at")
        if self.next_attempt_at is not None:
            _require_aware(self.next_attempt_at, "next_attempt_at")
        if self.next_attempt_at is not None and (
            self.escalated or self.explained or self.escalation != NO_DEBT
        ):
            raise ValueError("only a parked row can have been escalated")
        if self.explained and not self.escalated:
            raise ValueError("only an escalated park can have been explained")
        if self.last_planned_at is not None:
            _require_aware(self.last_planned_at, "last_planned_at")

    @property
    def parked(self) -> bool:
        return self.next_attempt_at is None

    @property
    def owes_escalation(self) -> bool:
        """A parked row with an issue whose block or comment has not landed."""
        return (
            self.parked
            and self.key.escalation_issue is not None
            and not (self.escalated and self.explained)
        )


class LivenessAnnouncement(StrEnum):
    """A timeline announcement the owner owes durably (#7350)."""

    PARKED = "parked"
    RELEASED = "released"


class Admission(StrEnum):
    """What the owner says about attempting a key now."""

    ADMIT = "admit"
    BACKING_OFF = "backing_off"
    PARKED = "parked"


def admission(row: LivenessRow | None, now: datetime) -> Admission:
    if row is None:
        return Admission.ADMIT
    if row.next_attempt_at is None:
        return Admission.PARKED
    return Admission.ADMIT if now >= row.next_attempt_at else Admission.BACKING_OFF


@dataclass(frozen=True, slots=True)
class LivenessPolicy:
    """Backoff and caps per outcome class. One policy for every action.

    * transient: ``base_backoff * 2**(attempts-1)``, capped at ``max_backoff``;
      parked when ``max_attempts`` failures with unchanged facts have been spent;
    * transient with a declared ``retry_at``: waits until then and spends
      nothing, for up to ``declared_wait_bound`` since the first failure. Past
      it the wait spends like any transient failure, so a limit that never
      lifts still parks;
    * permanent and needs-human: parked on the first occurrence.
    """

    max_attempts: int = 5
    base_backoff: timedelta = timedelta(minutes=1)
    max_backoff: timedelta = timedelta(minutes=30)
    #: The same bound the launch gate holds a GitHub rate limit for (#7303):
    #: one answer to "how long may a declared wait spend nothing".
    declared_wait_bound: timedelta = RATE_LIMIT_DEFERRAL_BOUND
    #: A row is SUPERSEDED once its operation has succeeded since the row was
    #: last asked about, and it has not been asked about for this long: its
    #: facts changed and the action now plans (and succeeds) under a new
    #: fingerprint. A sibling operation still being asked is never superseded.
    stale_after: timedelta = timedelta(hours=1)
    #: A row nobody has asked about for this long is ABANDONED: the action is no
    #: longer wanted. Long, so an action planned on a slow cadence (the stuck
    #: sweep runs every few hours) or across an engine pause keeps its budget.
    abandon_after: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_backoff <= timedelta(0) or self.max_backoff < self.base_backoff:
            raise ValueError("backoff must be positive and max_backoff >= base_backoff")
        if self.declared_wait_bound < timedelta(0):
            raise ValueError("declared_wait_bound cannot be negative")

    def effect_due(self, debt: EffectDebt, now: datetime) -> bool:
        """May the owner try an owed write (a block, a comment, a withdrawal,
        a pause) again?

        Not before its ``retry_at`` -- a rate limit's reset, or ``max_backoff``
        after a refusal -- and then always: an owed write is never abandoned,
        because dropping it is the failure it exists to prevent (a block left on
        an issue nobody will take off, a drifted issue never paused). The pace
        bounds its cost to one write per ``max_backoff``.
        """
        return debt.retry_at is None or now >= debt.retry_at

    def effect_after(
        self, debt: EffectDebt, result: EffectResult, now: datetime
    ) -> EffectDebt | None:
        """The debt one attempt leaves behind; ``None`` once it committed.

        A typed rate limit whose reset is still ahead waits until then and
        spends nothing -- for up to ``declared_wait_bound`` since the debt's
        first refusal, and never past it. Any other refusal, or a limit past
        the bound, spends an attempt and waits ``max_backoff``.
        """
        _require_aware(now, "now")
        if result.committed:
            return None
        first = debt.first_failed_at or now
        limit = result.rate_limit
        if (
            limit is not None
            and limit.resets_at > now
            and now - first < self.declared_wait_bound
        ):
            return replace(
                debt,
                retry_at=min(limit.resets_at, first + self.declared_wait_bound),
                first_failed_at=first,
            )
        return EffectDebt(
            attempts=debt.attempts + 1,
            attempted_at=now,
            retry_at=now + self.max_backoff,
            first_failed_at=first,
        )

    def backoff(self, attempts: int) -> timedelta:
        exponent = max(attempts - 1, 0)
        # Cap the exponent before multiplying: 2**attempts overflows timedelta.
        if exponent >= 32:
            return self.max_backoff
        return min(self.base_backoff * (2**exponent), self.max_backoff)

    def after(
        self,
        previous: LivenessRow | None,
        key: LivenessKey,
        outcome: ActionOutcome,
        now: datetime,
    ) -> LivenessRow | None:
        """The row this outcome leaves behind; ``None`` for done."""
        _require_aware(now, "now")
        if outcome.kind is OutcomeKind.DONE:
            return None
        spent = previous.attempts if previous is not None else 0
        first = previous.first_failed_at if previous is not None else now
        row = LivenessRow(
            key=key,
            attempts=spent,
            first_failed_at=first,
            last_failed_at=now,
            last_outcome=outcome.kind,
            last_reason=outcome.reason,
            next_attempt_at=None,
        )
        if previous is not None and previous.parked:
            # A park ends only by success or release (each of which withdraws
            # its block). An attempt run despite it -- an operator's explicit
            # recovery -- that does not succeed leaves it parked, escalation
            # and all: turning it into a wait would drop the park while its
            # block stayed on the issue.
            return replace(
                row,
                attempts=spent + 1,
                escalated=previous.escalated,
                explained=previous.explained,
                escalation=previous.escalation,
            )
        if outcome.kind is OutcomeKind.WAITING:
            return _backing_off(row, next_attempt_at=now + self.max_backoff)
        if outcome.kind is not OutcomeKind.TRANSIENT:
            return replace(row, attempts=spent + 1)
        # A declared wait spends nothing only while it is still ahead: a
        # reset already past would otherwise re-admit the action every tick.
        declared = (
            outcome.retry_at is not None
            and outcome.retry_at > now
            and now - first < self.declared_wait_bound
        )
        if declared:
            assert outcome.retry_at is not None
            # Never past the bound itself: a reset declared days ahead must not
            # hide the action in a wait that outlives the bound.
            bound = first + self.declared_wait_bound
            return _backing_off(row, next_attempt_at=min(outcome.retry_at, bound))
        spent += 1
        if spent >= self.max_attempts:
            return replace(
                row,
                attempts=spent,
                last_reason=(
                    f"{spent} attempts failed with unchanged facts; last: {outcome.reason}"
                ),
            )
        # Past the declared-wait bound a reset no longer sets the pace: the
        # ordinary ladder does, so a limit that never lifts reaches a person.
        return _backing_off(row, attempts=spent, next_attempt_at=now + self.backoff(spent))


def _backing_off(
    row: LivenessRow, *, next_attempt_at: datetime, attempts: int | None = None
) -> LivenessRow:
    """A row waiting to be retried: nothing about it has been escalated."""
    return replace(
        row,
        attempts=row.attempts if attempts is None else attempts,
        next_attempt_at=next_attempt_at,
        escalated=False,
        explained=False,
        escalation=NO_DEBT,
    )


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "ActionIdentity",
    "ActionOutcome",
    "Admission",
    "LivenessAnnouncement",
    "LivenessKey",
    "LivenessPolicy",
    "LivenessRow",
    "OutcomeKind",
    "admission",
    "fact_fingerprint",
]
