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
from functools import partial

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

logger = logging.getLogger(__name__)

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
                          record: ValidatedWorkRecord) -> RecoveryAttemptPending | None:
        """None when recovery owns this record; otherwise resolve it and say why."""
        perform = partial(self._effects.perform, token, claim)
        record_id = record.disposition.record_id
        rows = (record.current_evidence,
                *perform(lambda: self._store.attached_evidence(record_id)))
        roles = tuple(
            perform(partial(self._intake.prepare_evidence, row.admission.evidence)).role
            for row in rows
        )
        if any(recovery_owns(role) for role in roles):
            return None
        reason = outside_scope_reason(roles[0])
        retired = perform(lambda: self._store.retire_outside_scope(
            claim, evidence_ids=frozenset(row.evidence_id for row in rows),
            actor=RETIREMENT_ACTOR, reason=reason,
        ))
        if not retired:
            return RecoveryAttemptPending("Retained record changed before out-of-scope retirement")
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
        return RecoveryAttemptPending(
            f"Retired retained work outside recovery scope: {reason}; "
            f"block projection {projection.status.value}: {projection.message}"
        )


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
                 batch_size: int) -> None:
        require_positive(batch_size, "scope sweep batch size")
        self._source, self._store, self._execution = source, store, execution
        self._retirement, self._batch_size = retirement, batch_size
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
            try:
                if self._judge(request):
                    retired.append(request.record_id)
            except Exception:
                logger.exception("Recovery scope judgement failed for record %s", request.record_id)
        if len(requests) < self._batch_size:
            self._after = ""
        return RecoveryScopeSweepReport(tuple(retired))

    def _next(self) -> tuple[RecoveryRecordRequest, ...]:
        requests = self._source.unresolved_records(after_record_id=self._after, limit=self._batch_size)
        if not requests and self._after:
            self._after = ""
            requests = self._source.unresolved_records(after_record_id="", limit=self._batch_size)
        return requests

    def _judge(self, request: RecoveryRecordRequest) -> bool:
        lease = self._execution.try_enter(request.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return False
        with lease as token:
            if not self._execution.relinquish(token):
                return False
            try:
                record = self._store.record_for_id(request.record_id)
                if (record.current_evidence.evidence_id != request.evidence_id
                        or record.disposition.state not in UNRESOLVED_STATES):
                    return False
                # Prove before claiming: an in-scope record is never claimed here,
                # so this lane cannot contend with its publication or abandonment.
                if self._retirement.recovery_owns_record(record):
                    self._owned.add(request.evidence_id)
                    return False
                claim = self._store.acquire_claim(
                    request.record_id, expected_states=frozenset({record.disposition.state}),
                    evidence_id=request.evidence_id,
                )
                if claim is None:
                    return False
                self._execution.remember_claim(token, claim)
                if self._retirement.retire_if_outside(token, claim, record) is None:
                    self._owned.add(request.evidence_id)
                    return False
                return True
            finally:
                self._execution.relinquish(token)
