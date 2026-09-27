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
* fingerprint over the request itself - the record and its CURRENT evidence id,
  and for a refresh the authority snapshot, state and failure it was selected
  under. New evidence, a state change or a new observation revision is a new
  question; the same selection failing again spends from the same budget;
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
    fact_fingerprint,
)
from ..domain.recovery_attempt import RecoveryAttemptPending, RecoveryPendingKind
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest
from ..ports.validated_work_drain import ValidatedWorkDrainRequest
from ..ports.repository_host import host_rate_limit_of
from .action_liveness import ActionLivenessOwner, LivenessDecision, transient_outcome

RECOVER_ACTION = "recover_validated_work"
REFRESH_ACTION = "refresh_remote_authority"


def drain_subject(record_id: str) -> str:
    return f"validated_work:{record_id}"


def drain_outcome(
    result: RecoveryCompleted | RecoveryAttemptPending,
) -> ActionOutcome | None:
    """How one drain attempt ended; ``None`` when another owner held the record."""
    if isinstance(result, RecoveryCompleted):
        return ActionOutcome.done()
    failure = "" if result.failure is None else f" [{result.failure.value}]"
    reason = f"{result.message}{failure}"
    return _OUTCOMES[result.kind](reason)


#: One mapping from how a pending result says it should be counted to the
#: owner's vocabulary. Every kind is named: a new one fails here, not silently.
_OUTCOMES = {
    RecoveryPendingKind.FAILED: ActionOutcome.transient,
    RecoveryPendingKind.CONTENDED: lambda _reason: None,
    RecoveryPendingKind.WAITING: ActionOutcome.waiting,
    RecoveryPendingKind.NEEDS_HUMAN: ActionOutcome.needs_human,
}


@dataclass(frozen=True, slots=True)
class RecoveryDrainLiveness:
    """Keys drain requests and settles their outcomes through the one owner."""

    owner: ActionLivenessOwner
    #: The issue a record's work belongs to - where its park is escalated.
    record_issue: Callable[[str], int]

    def key(self, request: ValidatedWorkDrainRequest) -> LivenessKey:
        if isinstance(request, RemoteAuthorityRefreshRequest):
            action, issue = REFRESH_ACTION, request.authority.issue_number
        else:
            action, issue = RECOVER_ACTION, self.record_issue(request.record_id)
        return LivenessKey(
            identity=ActionIdentity(drain_subject(request.record_id), action),
            fingerprint=fact_fingerprint(request),
            escalation_issue=issue,
        )

    def admit(self, key: LivenessKey) -> LivenessDecision:
        return self.owner.admit(key)

    def settle(
        self, key: LivenessKey, result: RecoveryCompleted | RecoveryAttemptPending
    ) -> None:
        outcome = drain_outcome(result)
        if outcome is not None:
            self.owner.record(key, outcome)

    def settle_error(self, key: LivenessKey, error: Exception) -> None:
        self.owner.record(
            key,
            transient_outcome(f"{type(error).__name__}: {error}", host_rate_limit_of(error)),
        )


__all__ = [
    "RECOVER_ACTION",
    "REFRESH_ACTION",
    "RecoveryDrainLiveness",
    "drain_outcome",
    "drain_subject",
]
