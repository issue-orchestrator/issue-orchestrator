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

from collections.abc import Callable
from dataclasses import dataclass

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
from ..domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest
from ..ports.validated_work_drain import ValidatedWorkDrainRequest
from ..ports.repository_host import host_rate_limit_of
from .action_liveness import ActionLivenessOwner, LivenessDecision, transient_outcome

RECOVER_ACTION = "recover_validated_work"
REFRESH_ACTION = "refresh_remote_authority"
SCOPE_ACTION = "judge_record_scope"


def drain_subject(record_id: str) -> str:
    return f"validated_work:{record_id}"


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
}


@dataclass(frozen=True, slots=True)
class RecoveryDrainLiveness:
    """Keys drain requests and settles their outcomes through the one owner."""

    owner: ActionLivenessOwner
    #: A record's current disposition: its issue (where a park is escalated)
    #: and its durable state (a fact of the question). One read, one owner.
    record_disposition: Callable[[str], ValidatedWorkDisposition]

    def key(self, request: ValidatedWorkDrainRequest) -> LivenessKey:
        if isinstance(request, RemoteAuthorityRefreshRequest):
            return self._key(REFRESH_ACTION, request.record_id,
                             request.authority.issue_number, request)
        # The record, its current evidence and its durable state are the facts.
        # An operator's approval is authority to run, not a fact: the explicit
        # recovery of a parked selection is the same question, and its success
        # must settle that park.
        return self._record_key(RECOVER_ACTION, request.record_id, request.evidence_id)

    def scope_key(self, request: RecoveryRecordRequest) -> LivenessKey:
        """The scope sweep's judgement of one record (#7323's lane)."""
        return self._record_key(SCOPE_ACTION, request.record_id, request.evidence_id)

    def _record_key(self, action: str, record_id: str, evidence_id: str) -> LivenessKey:
        disposition = self.record_disposition(record_id)
        facts = {"record_id": record_id, "evidence_id": evidence_id, "state": disposition.state}
        return self._key(action, record_id, disposition.key.issue_number, facts)

    @staticmethod
    def _key(action: str, record_id: str, issue: int, facts: object) -> LivenessKey:
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
        self._record(key, drain_outcome(result))

    def judged(self, key: LivenessKey) -> None:
        """The scope sweep reached a judgement (retired, in scope, or held)."""
        self._record(key, ActionOutcome.done())

    def _record(self, key: LivenessKey, outcome: ActionOutcome) -> None:
        self.owner.record(key, outcome)
        if outcome.kind is OutcomeKind.DONE:
            # Done: no question this action asked about the record is still
            # open, including ones asked under its older evidence or state.
            self.owner.release_identity(key.identity)

    def settle_error(self, key: LivenessKey, error: Exception) -> None:
        self.owner.record(
            key,
            transient_outcome(f"{type(error).__name__}: {error}", host_rate_limit_of(error)),
        )


__all__ = [
    "RECOVER_ACTION",
    "REFRESH_ACTION",
    "SCOPE_ACTION",
    "RecoveryDrainLiveness",
    "drain_outcome",
    "drain_subject",
]
