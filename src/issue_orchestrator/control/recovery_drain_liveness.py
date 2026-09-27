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
from typing import Protocol, TypeVar

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

_Fact = TypeVar("_Fact")

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

    def key(self, request: ValidatedWorkDrainRequest) -> LivenessKey | None:
        """The key a drain request runs under; ``None`` when a fact read for it
        just failed (that failure is already settled: nothing else to run).

        The record, its current evidence and its durable state are the facts.
        An operator's approval is authority to run, not a fact: the explicit
        recovery of a parked selection is the same question, and its success
        must settle that park.
        """
        if isinstance(request, RemoteAuthorityRefreshRequest):
            return self._key(REFRESH_ACTION, request.record_id,
                             request.authority.issue_number, request)
        disposition = self._disposition(RECOVER_ACTION, request)
        if disposition is None or isinstance(disposition, LivenessKey):
            return disposition
        return self._record_key(RECOVER_ACTION, request.record_id, request.evidence_id, disposition)

    def scope_key(self, request: RecoveryRecordRequest) -> LivenessKey | None:
        """The scope sweep's judgement of one record (#7323's lane), over every
        evidence row it reads: newly attached evidence is a new question."""
        record_id, evidence_id = request.record_id, request.evidence_id
        disposition = self._disposition(SCOPE_ACTION, request)
        if disposition is None or isinstance(disposition, LivenessKey):
            return disposition
        attached = self._guarded_read(
            self._record_key(SCOPE_ACTION, record_id, evidence_id, disposition, attached=_UNREADABLE),
            lambda: frozenset(row.evidence_id for row in self.records.attached_evidence(record_id)),
            f"Attached evidence of record {record_id}",
        )
        if attached is None or isinstance(attached, LivenessKey):
            return attached
        return self._record_key(SCOPE_ACTION, record_id, evidence_id, disposition, attached=attached)

    def _disposition(
        self, action: str, request: RecoveryRecordRequest
    ) -> ValidatedWorkDisposition | LivenessKey | None:
        """The record's disposition, read under its own stable "unreadable" key,
        escalated on the issue the durable selection named. A disposition that
        names another issue than its selection is a failed read too."""
        record_id, selected_issue = request.record_id, request.issue_number
        unreadable = self._key(action, record_id, selected_issue, {
            "record_id": record_id, "evidence_id": request.evidence_id, "state": _UNREADABLE,
        })

        def read() -> ValidatedWorkDisposition:
            disposition = self.records.get(record_id)
            if selected_issue is not None and disposition.key.issue_number != selected_issue:
                raise ValueError(
                    f"record {record_id} was selected under issue #{selected_issue} but its"
                    f" disposition names #{disposition.key.issue_number}"
                )
            return disposition

        return self._guarded_read(unreadable, read, f"Disposition of record {record_id}")

    def _guarded_read(
        self, unreadable: LivenessKey, read: Callable[[], _Fact], what: str
    ) -> _Fact | LivenessKey | None:
        """Read one fact of a key under the stable key of its read failing.

        A failed read is a fact of its own, and each is that key's attempt,
        settled here (``None``). The read is made only while that durable key
        is admitted -- its backoff paces the reads and its park stops them,
        across restarts, until an operator releases it (the held key is then
        returned, which its caller's admission holds too). A read that
        succeeds answers that question, so its row goes.
        """
        decision = self.owner.admit(unreadable)
        if not decision.admitted:
            return unreadable
        try:
            fact = read()
        except Exception as error:
            logger.warning("%s is unreadable", what, exc_info=True)
            self.owner.record(
                unreadable,
                transient_outcome(f"{type(error).__name__}: {error}", host_rate_limit_of(error)),
            )
            return None
        if decision.row is not None:
            self.owner.record(unreadable, ActionOutcome.done())
        return fact

    def _record_key(
        self,
        action: str,
        record_id: str,
        evidence_id: str,
        disposition: ValidatedWorkDisposition,
        **extra: object,
    ) -> LivenessKey:
        facts = {
            "record_id": record_id, "evidence_id": evidence_id,
            "state": disposition.state, **extra,
        }
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

    def settle_explicit(
        self,
        record_id: str,
        key: LivenessKey | None,
        outcome: RecoveryCompleted | RecoveryAttemptPending | Exception,
    ) -> None:
        """Settle an operator's explicit recovery, which runs whatever the
        owner holds -- even with no key, when a fact read for it just failed
        (that failure is already counted under its own key). A recovery that
        resolved the record still releases every lane either way."""
        if key is not None:
            if isinstance(outcome, Exception):
                self.settle_error(key, outcome)
            else:
                self.settle(key, outcome)
            return
        if isinstance(outcome, RecoveryCompleted) or (
            isinstance(outcome, RecoveryAttemptPending)
            and outcome.kind is RecoveryPendingKind.RESOLVED
        ):
            self.resolve_record(record_id)
            return
        self.resolve_if_terminal(record_id)

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
