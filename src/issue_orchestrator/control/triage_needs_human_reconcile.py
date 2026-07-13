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
        """
        marker = AddLabelAction(
            issue_number=issue_number,
            label=self._labels.triage_needs_human,
            reason=reason,
        )
        if not self._apply_actions([marker], context):
            return False

        needs_human = AddLabelAction(
            issue_number=issue_number,
            label=self._labels.needs_human,
            reason=reason,
        )
        if not self._apply_actions([needs_human], context):
            return False

        note = AddCommentAction(number=issue_number, comment=comment, reason=reason)
        if not self._apply_actions([note], context):
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
            # idempotent pass cleans below.
            if self._labels.needs_human in current:
                cleared = self._apply_actions(
                    [
                        RemoveLabelAction(
                            issue_number=issue_number,
                            label=self._labels.needs_human,
                            reason=(
                                "running triage investigation supersedes "
                                "orchestrator-owned needs-human escalation"
                            ),
                        )
                    ],
                    "triage_reconcile_needs_human",
                )
                if not cleared:
                    continue

            self._apply_actions(
                [
                    RemoveLabelAction(
                        issue_number=issue_number,
                        label=self._labels.triage_needs_human,
                        reason="triage needs-human escalation no longer active",
                    )
                ],
                "triage_reconcile_needs_human_marker",
            )
