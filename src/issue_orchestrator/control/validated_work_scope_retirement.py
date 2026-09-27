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
from ..domain.validated_work import ResolutionKind
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken
from ..domain.validated_work_scope import outside_scope_reason, recovery_owns
from ..domain.validated_work_store import ValidatedWorkRecord
from ..events import EventName
from ..ports.completion_intake import CompletionIntakeLedger
from ..ports.event_sink import EventSink, make_trace_event
from ..ports.recovery_block import RecoveryBlockIssueReconciler
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_store import ValidatedWorkStore

logger = logging.getLogger(__name__)

RETIREMENT_ACTOR = "orchestrator:validated-work-scope"


class OutOfScopeRecordRetirement:
    def __init__(self, *, intake: CompletionIntakeLedger, store: ValidatedWorkStore,
                 effects: ValidatedWorkEffectAuthority, blocks: RecoveryBlockIssueReconciler,
                 events: EventSink) -> None:
        self._intake, self._store, self._effects = intake, store, effects
        self._blocks, self._events = blocks, events

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
