"""Resolve retained records recovery never owned, then release their block (#7323).

Capture now refuses completions outside recovery scope (`validated_work_scope`).
Records admitted before that rule -- a tech-lead health review or failure
investigation captured as its subject's work -- still hold ``recovery-pending``
and would drain forever: a PUBLISHING record cannot even be abandoned by an
operator. Recovery asks this owner first, under the record's claim, so such a
record resolves with a named reason instead of being published.

The proof is the exact owner custody (`prepare_evidence`), never a branch name
or a label: a record resolves only when EVERY current and attached evidence it
holds is outside scope, and the store refuses if that set changed meanwhile.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from functools import partial

from ..domain.action_liveness import ActionOutcome
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_drain import RecoveryDrainMode, RecoveryScopeSweepReport
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work import UNRESOLVED_STATES, ResolutionKind, require_positive
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionBusy, RecordExecutionToken
from ..domain.validated_work_scope import outside_scope_reason, recovery_owns
from ..domain.validated_work_store import ValidatedWorkRecord
from ..events import EventName
from ..ports.completion_intake import CompletionIntakeLedger
from ..ports.event_sink import EventSink, make_trace_event
from ..ports.recovery_block import RecoveryBlockIssueReconciler
from ..ports.validated_work_drain import RecoveryDrainAdmission, ValidatedWorkScopeSource
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner
from ..ports.validated_work_store import ValidatedWorkStore

if TYPE_CHECKING:
    from .recovery_drain_liveness import RecoveryDrainLiveness

logger = logging.getLogger(__name__)


class ScopeRetirementStatus(StrEnum):
    IN_SCOPE = "in_scope"  # recovery owns this record; nothing was written
    RETIRED = "retired"  # resolved ABANDONED / outside_recovery_scope
    CHANGED = "changed"  # the store CAS refused: evidence or claim moved


@dataclass(frozen=True, slots=True)
class ScopeRetirement:
    """What `retire_if_outside` did. Only RETIRED is a state transition."""

    status: ScopeRetirementStatus
    message: str

    @property
    def outside_scope(self) -> bool:
        return self.status is not ScopeRetirementStatus.IN_SCOPE

    def pending(self) -> RecoveryAttemptPending:
        """The drain lanes' answer for a record they must not publish or refresh."""
        if not self.outside_scope:
            raise ValueError("an in-scope record continues through recovery")
        return RecoveryAttemptPending(self.message)

RETIREMENT_ACTOR = "orchestrator:validated-work-scope"


class OutOfScopeRecordRetirement:
    def __init__(self, *, intake: CompletionIntakeLedger, store: ValidatedWorkStore,
                 effects: ValidatedWorkEffectAuthority, blocks: RecoveryBlockIssueReconciler,
                 events: EventSink) -> None:
        self._intake, self._store, self._effects = intake, store, effects
        self._blocks, self._events = blocks, events

    def recovery_owns_record(self, record: ValidatedWorkRecord) -> bool:
        """Unfenced read-only proof, for callers deciding whether to claim at all."""
        rows = (record.current_evidence,
                *self._store.attached_evidence(record.disposition.record_id))
        return any(
            recovery_owns(self._intake.prepare_evidence(row.admission.evidence).role)
            for row in rows
        )

    def retire_if_outside(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                          record: ValidatedWorkRecord) -> ScopeRetirement:
        """Resolve the record when recovery never owned it; say exactly what happened."""
        perform = partial(self._effects.perform, token, claim)
        record_id = record.disposition.record_id
        rows = (record.current_evidence,
                *perform(lambda: self._store.attached_evidence(record_id)))
        roles = tuple(
            perform(partial(self._intake.prepare_evidence, row.admission.evidence)).role
            for row in rows
        )
        if any(recovery_owns(role) for role in roles):
            return ScopeRetirement(ScopeRetirementStatus.IN_SCOPE, "recovery owns this record")
        reason = outside_scope_reason(roles[0])
        retired = perform(lambda: self._store.retire_outside_scope(
            claim, evidence_ids=frozenset(row.evidence_id for row in rows),
            actor=RETIREMENT_ACTOR, reason=reason,
        ))
        if not retired:
            return ScopeRetirement(ScopeRetirementStatus.CHANGED,
                                   "Retained record changed before out-of-scope retirement")
        issue = record.disposition.key.issue_number
        logger.info("[VALIDATED_WORK] Retired record %s of issue #%d: %s", record_id, issue, reason)
        self._events.publish(make_trace_event(EventName.VALIDATED_WORK_ABANDONED, {
            "issue_number": issue,
            "record_id": record_id,
            "evidence_id": record.current_evidence.evidence_id,
            "actor": RETIREMENT_ACTOR,
            "reason": reason,
            "resolution_kind": ResolutionKind.OUTSIDE_RECOVERY_SCOPE.value,
        }))
        # The resolved record no longer holds recovery. A busy or failed
        # projection is healed by the drain's block sweep, which revisits every
        # issue with retained evidence.
        projection = perform(lambda: self._blocks.reconcile_issue_block(issue))
        return ScopeRetirement(
            ScopeRetirementStatus.RETIRED,
            f"Retired retained work outside recovery scope: {reason}; "
            f"block projection {projection.status.value}: {projection.message}",
        )


@dataclass(frozen=True, slots=True)
class _Judgement:
    """What judging one record came to, as the liveness owner counts it."""

    outcome: ActionOutcome
    retired: bool = False

    @classmethod
    def held(cls, reason: str) -> "_Judgement":
        """Another owner holds the record: not a failure, but shown and paced,
        since some records the sweep judges no publication lane selects."""
        return cls(ActionOutcome.waiting(reason))


#: The record moved on (new evidence, or resolved) before it was judged: the
#: question is answered.
_MOVED_ON = _Judgement(ActionOutcome.done())
#: The retirement owner's typed outcome. A refused retirement (CHANGED)
#: repeated under unchanged facts is a loop, so it spends a budget.
_BY_STATUS = {
    ScopeRetirementStatus.IN_SCOPE: _Judgement(ActionOutcome.done()),
    ScopeRetirementStatus.RETIRED: _Judgement(ActionOutcome.done(), retired=True),
    ScopeRetirementStatus.CHANGED: _Judgement(ActionOutcome.transient(
        "Scope retirement was refused: evidence or claim moved"
    )),
}


class OutOfScopeRetirementSweep:
    """Judge EVERY unresolved record's scope, not just the publication lanes'.

    The drain's publication and refresh lanes consult the retirement owner for
    the records they select. PARKED records awaiting approval, FAILED records and
    non-HEAD lineage peers are selected by neither, yet a pre-rule tech-lead
    capture in those states blocks its issue the same way. This bounded,
    round-robin lane reaches them. A record proven in scope is remembered by
    its current evidence id -- evidence and its run role are immutable -- so it
    is proven once per process, not once per tick.
    """

    def __init__(self, *, source: ValidatedWorkScopeSource, store: ValidatedWorkStore,
                 execution: ValidatedWorkExecutionOwner, retirement: OutOfScopeRecordRetirement,
                 batch_size: int, liveness: "RecoveryDrainLiveness") -> None:
        require_positive(batch_size, "scope sweep batch size")
        self._source, self._store, self._execution = source, store, execution
        self._retirement, self._batch_size = retirement, batch_size
        self._liveness = liveness
        self._owned: set[str] = set()
        self._after = ""

    def tick(self, admission: RecoveryDrainAdmission) -> RecoveryScopeSweepReport:
        try:
            requests = self._next()
        except Exception as error:
            return RecoveryScopeSweepReport((), f"Retained record scan failed: {error}")
        retired: list[str] = []
        for request in requests:
            if admission() is RecoveryDrainMode.STOPPED:
                break
            self._after = request.record_id
            if request.evidence_id in self._owned:
                continue
            if self._judge_bounded(request):
                retired.append(request.record_id)
        if len(requests) < self._batch_size:
            self._after = ""
        return RecoveryScopeSweepReport(tuple(retired))

    def _next(self) -> tuple[RecoveryRecordRequest, ...]:
        requests = self._source.unresolved_records(after_record_id=self._after, limit=self._batch_size)
        if not requests and self._after:
            self._after = ""
            requests = self._source.unresolved_records(after_record_id="", limit=self._batch_size)
        return requests

    def _judge_bounded(self, request: RecoveryRecordRequest) -> bool:
        """Judge one record through the drain's liveness (#7350); whether it
        was retired. A judgement that fails the same way every pass is held,
        then parked, like any other replanned action."""
        key = self._liveness.scope_key(request)
        if key is None or not self._liveness.admit(key).admitted:
            return False
        try:
            judgement = self._judge(request)
        except Exception as error:
            logger.exception("Recovery scope judgement failed for record %s", request.record_id)
            self._liveness.settle_error(key, error)
            return False
        self._liveness.record(key, judgement.outcome)
        return judgement.retired

    def _judge(self, request: RecoveryRecordRequest) -> "_Judgement":
        """What judging the record came to, in the liveness owner's terms."""
        lease = self._execution.try_enter(request.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return _Judgement.held("Record is executing under another owner")
        with lease as token:
            if not self._execution.relinquish(token):
                return _Judgement.held("Record awaits its reserved stop operation")
            try:
                record = self._store.record_for_id(request.record_id)
                if (record.current_evidence.evidence_id != request.evidence_id
                        or record.disposition.state not in UNRESOLVED_STATES):
                    return _MOVED_ON
                # Prove before claiming: an in-scope record is never claimed here,
                # so this lane cannot contend with its publication or abandonment.
                if self._retirement.recovery_owns_record(record):
                    self._owned.add(request.evidence_id)
                    return _BY_STATUS[ScopeRetirementStatus.IN_SCOPE]
                claim = self._store.acquire_claim(
                    request.record_id, expected_states=frozenset({record.disposition.state}),
                    evidence_id=request.evidence_id,
                )
                if claim is None:
                    return _Judgement.held("Record's claim is held by another owner")
                self._execution.remember_claim(token, claim)
                outcome = self._retirement.retire_if_outside(token, claim, record)
                if outcome.status is ScopeRetirementStatus.IN_SCOPE:
                    self._owned.add(request.evidence_id)
                return _BY_STATUS[outcome.status]
            finally:
                self._execution.relinquish(token)
