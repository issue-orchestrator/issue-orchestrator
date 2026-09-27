"""The recovery drain's half of action liveness (#7350).

The drain re-selects every retained record in ``queued``/``publishing`` (and
some ``parked`` ones for an authority refresh) on every interval, and until this
module the result of each attempt evaporated: ``RecoveryAttemptPending`` went
into a report the tick threw away. Census loops #5 (a permanent publish failure
retried every drain pass - 343 times in one day on io), #6 (a completion
rejected ``missing_authority`` on every pass) and #7 (zero-commit health-review
records re-selected forever) were all that.

Each drain attempt is keyed like any other action:

* subject ``validated_work:<record_id>``;
* action ``recover_validated_work`` or ``refresh_remote_authority``;
* fingerprint over the request itself - the record, its CURRENT evidence id and
  its durable state, and for a refresh the authority snapshot, state and
  failure it was selected under. New evidence, a state change or a new
  observation revision is a new question; the same selection failing again
  spends from the same budget. A recovery that completes answers every
  question about its record, so it releases the record's older rows too;
* escalation issue: the record's issue, so a park is visible where the work is.

Parking never touches the record. Validated commits, evidence and the record's
own state stay exactly as they were; the drain only stops selecting it until
its facts change, it recovers, or an operator retries the issue.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    LivenessKey,
    OutcomeKind,
    fact_fingerprint,
)
from ..domain.recovery_attempt import RecoveryAttemptPending, RecoveryPendingKind
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import ValidatedWorkDisposition
from ..domain.validated_work_store import EvidenceRow
from ..domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest
from ..ports.validated_work_drain import ValidatedWorkDrainRequest
from ..ports.repository_host import host_rate_limit_of
from .action_liveness import ActionLivenessOwner, LivenessDecision, transient_outcome

logger = logging.getLogger(__name__)

#: The scope facts of a record whose attached evidence could not be read.
_UNREADABLE = "unreadable"

RECOVER_ACTION = "recover_validated_work"
REFRESH_ACTION = "refresh_remote_authority"
SCOPE_ACTION = "judge_record_scope"


_SUBJECT_PREFIX = "validated_work:"


def drain_subject(record_id: str) -> str:
    return f"{_SUBJECT_PREFIX}{record_id}"


def _record_id(key: LivenessKey) -> str:
    return key.identity.subject.removeprefix(_SUBJECT_PREFIX)


def drain_outcome(result: RecoveryCompleted | RecoveryAttemptPending) -> ActionOutcome:
    """How one drain attempt ended, in the owner's vocabulary."""
    if isinstance(result, RecoveryCompleted):
        return ActionOutcome.done()
    failure = "" if result.failure is None else f" [{result.failure.value}]"
    reason = f"{result.message}{failure}"
    if result.failure in _DETERMINISTIC_REFUSALS:
        return ActionOutcome.permanent(reason)
    return _OUTCOMES[result.kind](reason, result)


#: Failures the same selection will meet every time (#7357's typed PR-create
#: refusals): parked on the first occurrence rather than after a budget.
_DETERMINISTIC_REFUSALS = frozenset(
    {ValidatedWorkFailure.PR_CREATE_NO_COMMITS, ValidatedWorkFailure.PR_CREATE_REJECTED}
)


#: One mapping from how a pending result says it should be counted to the
#: owner's vocabulary. Every kind is named: a new one fails here, not silently.
_OUTCOMES: dict[
    RecoveryPendingKind, Callable[[str, RecoveryAttemptPending], ActionOutcome]
] = {
    # A failed remote read keeps the host's typed rate limit, so it waits for
    # the reset instead of spending budget.
    RecoveryPendingKind.FAILED: lambda reason, result: transient_outcome(
        reason, result.rate_limit
    ),
    # Held by another owner: spends nothing, but is shown and paced, so a
    # holder that never lets go cannot hide the record.
    RecoveryPendingKind.CONTENDED: lambda reason, _result: ActionOutcome.waiting(reason),
    RecoveryPendingKind.WAITING: lambda reason, _result: ActionOutcome.waiting(reason),
    RecoveryPendingKind.NEEDS_HUMAN: lambda reason, _result: ActionOutcome.needs_human(reason),
    RecoveryPendingKind.ADVANCED: lambda _reason, _result: ActionOutcome.done(),
    RecoveryPendingKind.RESOLVED: lambda _reason, _result: ActionOutcome.done(),
}


class RecordFacts(Protocol):
    """The record facts a drain key is made of: the validated-work store.

    Only the disposition and the attached evidence rows -- never the full
    record, whose read is the operation's own and may fail after admission,
    where that failure is settled and bounded under a stable key.
    """

    def get(self, record_id: str) -> ValidatedWorkDisposition: ...

    def attached_evidence(self, record_id: str) -> tuple[EvidenceRow, ...]: ...


@dataclass(frozen=True, slots=True)
class RecoveryDrainLiveness:
    """Keys drain requests and settles their outcomes through the one owner."""

    owner: ActionLivenessOwner
    #: A record's disposition gives its issue (where a park is escalated) and
    #: its durable state; its attached evidence, which the scope judgement
    #: also reads, is a fact of that question.
    records: RecordFacts

    def key(self, request: ValidatedWorkDrainRequest) -> LivenessKey:
        if isinstance(request, RemoteAuthorityRefreshRequest):
            return self._key(REFRESH_ACTION, request.record_id,
                             request.authority.issue_number, request)
        # The record, its current evidence and its durable state are the facts.
        # An operator's approval is authority to run, not a fact: the explicit
        # recovery of a parked selection is the same question, and its success
        # must settle that park.
        return self._record_key(RECOVER_ACTION, request.record_id, request.evidence_id)

    def scope_key(self, request: RecoveryRecordRequest) -> LivenessKey | None:
        """The scope sweep's judgement of one record (#7323's lane), over every
        evidence row it reads: newly attached evidence is a new question.

        Attached evidence that cannot be read is a fact of its own, under a
        stable "unreadable" key, and each failed read is that key's attempt,
        settled here (``None``: nothing else to run). The read is made only
        while that durable key is admitted -- its backoff paces the reads and
        its park stops them, across restarts, until an operator releases it.
        A read that succeeds is a new question.
        """
        record_id = request.record_id
        unreadable = self._scope_key(request, _UNREADABLE)
        if not self.owner.admit(unreadable).admitted:
            return unreadable
        try:
            attached = frozenset(row.evidence_id for row in self.records.attached_evidence(record_id))
        except Exception as error:
            logger.warning("Attached evidence of record %s is unreadable", record_id, exc_info=True)
            self.settle_error(unreadable, error)
            return None
        return self._scope_key(request, attached)

    def _scope_key(self, request: RecoveryRecordRequest, attached: object) -> LivenessKey:
        return self._record_key(
            SCOPE_ACTION, request.record_id, request.evidence_id, attached=attached
        )

    def _record_key(
        self, action: str, record_id: str, evidence_id: str, **extra: object
    ) -> LivenessKey:
        facts: dict[str, object] = {"record_id": record_id, "evidence_id": evidence_id, **extra}
        try:
            disposition = self.records.get(record_id)
        except Exception:
            # A record selection returned but whose disposition cannot be read:
            # still keyed, stably, so its operation (which reads the record
            # itself, after admission) is bounded; with no readable issue its
            # park escalates nowhere but the board and the CLI.
            logger.warning("Disposition of record %s is unreadable", record_id, exc_info=True)
            return self._key(action, record_id, None, {**facts, "state": _UNREADABLE})
        facts["state"] = disposition.state
        return self._key(action, record_id, disposition.key.issue_number, facts)

    @staticmethod
    def _key(action: str, record_id: str, issue: int | None, facts: object) -> LivenessKey:
        return LivenessKey(
            identity=ActionIdentity(drain_subject(record_id), action),
            fingerprint=fact_fingerprint(facts),
            escalation_issue=issue,
        )

    def admit(self, key: LivenessKey) -> LivenessDecision:
        return self.owner.admit(key)

    def settle(
        self, key: LivenessKey, result: RecoveryCompleted | RecoveryAttemptPending
    ) -> None:
        record_id = _record_id(key)
        if isinstance(result, RecoveryCompleted) or result.kind is RecoveryPendingKind.RESOLVED:
            self.record(key, ActionOutcome.done())
            self.resolve_record(record_id)
            return
        if not self.resolve_if_terminal(record_id):
            self.record(key, drain_outcome(result))

    def resolve_if_terminal(self, record_id: str) -> bool:
        """Whether the record is durably resolved -- then every lane's rows
        are released -- whatever the attempt that just ran reported: a step
        after a committed recovery or retirement can still fail or pend.
        An unreadable disposition counts as not resolved."""
        try:
            resolved = not self.records.get(record_id).unresolved
        except Exception:
            logger.warning("Disposition of record %s is unreadable", record_id, exc_info=True)
            return False
        if resolved:
            self.resolve_record(record_id)
        return resolved

    def resolve_record(self, record_id: str) -> None:
        """The record is resolved (recovered or retired): release every drain
        lane's rows about it, and owe the withdrawal of their blocks."""
        for action in (RECOVER_ACTION, REFRESH_ACTION, SCOPE_ACTION):
            self.owner.release_identity(ActionIdentity(drain_subject(record_id), action))

    def record(self, key: LivenessKey, outcome: ActionOutcome) -> None:
        """Settle one attempt of a drain lane's action."""
        self.owner.record(key, outcome)
        if outcome.kind is OutcomeKind.DONE:
            # Done: no question this action asked about the record is still
            # open, including ones asked under its older evidence or state.
            self.owner.release_identity(key.identity)

    def settle_error(self, key: LivenessKey, error: Exception) -> bool:
        """Settle an attempt that raised; whether its record turned out resolved."""
        if self.resolve_if_terminal(_record_id(key)):
            return True
        self.owner.record(
            key,
            transient_outcome(f"{type(error).__name__}: {error}", host_rate_limit_of(error)),
        )
        return False


__all__ = [
    "RECOVER_ACTION",
    "REFRESH_ACTION",
    "SCOPE_ACTION",
    "RecoveryDrainLiveness",
    "drain_outcome",
    "drain_subject",
]
