"""Label-owned needs-human lifecycle for triage launch exhaustion.

The provenance marker and the needs-human label live together on GitHub, the
repository's crash-safe source of truth.  No local clear-obligation store is
needed: every tick compares those labels with the running/restored sessions and
idempotently removes an escalation that active work has superseded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .reconciliation import ExpectedState, ReconciliationRequired

if TYPE_CHECKING:
    from ..domain.models import Session
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

ApplyActions = Callable[[list[Action], str], bool]
ReadLabels = Callable[[int], list[str]]


class TriageNeedsHumanLifecycle:
    """Own triage needs-human escalation and stale-label reconciliation."""

    def __init__(
        self,
        *,
        labels: "LabelManager",
        events: EventSink,
        read_labels: ReadLabels,
        apply_actions: ApplyActions,
    ) -> None:
        self._labels = labels
        self._events = events
        self._read_labels = read_labels
        self._apply_actions = apply_actions

    def _expected(
        self,
        *,
        required: set[str] | None = None,
        forbidden: set[str] | None = None,
    ) -> ExpectedState:
        """Guard an owned mutation with marker-provenance invariants.

        Every lifecycle mutation is optimistic: the applier re-reads live
        labels and refuses the write (``ReconciliationRequired``) unless they
        still satisfy this expected state.  The reconcile pause label is always
        forbidden so a paused issue fails closed, and it is resolved through
        ``LabelManager`` rather than hard-coded so the configured prefix is
        honored.
        """
        forbidden_labels = {self._labels.needs_reconcile}
        if forbidden:
            forbidden_labels |= forbidden
        return ExpectedState.with_labels(
            required=required or set(),
            forbidden=forbidden_labels,
        )

    def _apply_guarded(self, actions: list[Action], context: str) -> bool:
        """Apply owned mutations, treating expected-state drift as retryable.

        A ``ReconciliationRequired`` means the labels changed between our read
        and the write — a human cleared needs-human, another path paused the
        issue, or the marker vanished.  We must not force the mutation past the
        stale assumption: skip it and let the next idempotent tick re-evaluate
        against fresh state.  Returning ``False`` matches the "did not apply"
        signal callers already handle, so the escalation withholds its event
        and the reconcile keeps its provenance marker.
        """
        try:
            return self._apply_actions(actions, context)
        except ReconciliationRequired as drift:
            logger.info(
                "[TRIAGE] %s skipped: labels drifted since read (%s); "
                "retrying next tick",
                context,
                drift.reason,
            )
            return False

    def escalate(
        self,
        *,
        issue_number: int,
        reason: str,
        comment: str,
        context: str,
        event_data: dict[str, object],
    ) -> bool:
        """Apply provenance, blocking state, and explanation in safe order.

        The marker commits first.  Therefore a needs-human label created by this
        workflow can never exist without durable ownership provenance.  Each
        mutation is idempotent, so a partial attempt remains queued and simply
        resumes at the same ordered sequence on a later tick.

        Each step also carries an expected-state guard so a concurrent human or
        orchestrator path that clears or pauses the escalation between our steps
        stops the sequence instead of stamping needs-human or a comment onto
        state that no longer holds: the marker add refuses a paused issue, the
        needs-human add requires the marker to still be present, and the
        durable comment/event requires both the marker and needs-human.
        """
        marker = AddLabelAction(
            issue_number=issue_number,
            label=self._labels.triage_needs_human,
            reason=reason,
            expected=self._expected(),
        )
        if not self._apply_guarded([marker], context):
            return False

        needs_human = AddLabelAction(
            issue_number=issue_number,
            label=self._labels.needs_human,
            reason=reason,
            expected=self._expected(required={self._labels.triage_needs_human}),
        )
        if not self._apply_guarded([needs_human], context):
            return False

        note = AddCommentAction(
            number=issue_number,
            comment=comment,
            reason=reason,
            expected=self._expected(
                required={self._labels.triage_needs_human, self._labels.needs_human}
            ),
        )
        if not self._apply_guarded([note], context):
            return False

        self._events.publish(
            make_trace_event(EventName.ISSUE_NEEDS_HUMAN, dict(event_data))
        )
        return True

    def reconcile(self, active_sessions: Sequence["Session"]) -> None:
        """Clear marker-owned escalations superseded by active/restored work.

        Only the marker proves ownership.  A bare needs-human label is operator-
        or session-owned and is never touched.  Reads bypass caches because a
        stale label observation could remove legitimate human intent.
        """
        issue_numbers = sorted({session.issue.number for session in active_sessions})
        for issue_number in issue_numbers:
            try:
                current = set(self._read_labels(issue_number))
            except Exception:
                logger.exception(
                    "[TRIAGE] Failed to read fresh labels while reconciling issue #%d; "
                    "will retry next tick",
                    issue_number,
                )
                continue

            if self._labels.triage_needs_human not in current:
                continue

            # Keep provenance until the blocking label is definitely gone.  If
            # removal fails, the marker survives and the next tick retries.  A
            # crash after this succeeds leaves marker-only state, which the next
            # idempotent pass cleans below.  Both removals are guarded against
            # the labels we read above: the fresh read is only a hint, so the
            # applier re-checks live state and refuses to strip needs-human
            # unless the marker still owns it, or to strip the marker unless
            # needs-human is already gone.  Drift skips this issue for one tick.
            if self._labels.needs_human in current:
                cleared = self._apply_guarded(
                    [
                        RemoveLabelAction(
                            issue_number=issue_number,
                            label=self._labels.needs_human,
                            reason=(
                                "running triage investigation supersedes "
                                "orchestrator-owned needs-human escalation"
                            ),
                            expected=self._expected(
                                required={
                                    self._labels.triage_needs_human,
                                    self._labels.needs_human,
                                }
                            ),
                        )
                    ],
                    "triage_reconcile_needs_human",
                )
                if not cleared:
                    continue

            self._apply_guarded(
                [
                    RemoveLabelAction(
                        issue_number=issue_number,
                        label=self._labels.triage_needs_human,
                        reason="triage needs-human escalation no longer active",
                        expected=self._expected(
                            required={self._labels.triage_needs_human},
                            forbidden={self._labels.needs_human},
                        ),
                    )
                ],
                "triage_reconcile_needs_human_marker",
            )
