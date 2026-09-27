"""The action liveness owner (#7350): nothing retries forever.

Every replanning path - the planner's applied actions, the recovery drain - asks
this owner two questions about an action keyed on (subject, action, fact
fingerprint):

* :meth:`ActionLivenessOwner.admit` BEFORE it tries: may this run now, is it
  backing off, or is it parked?
* :meth:`ActionLivenessOwner.record` AFTER it tried: here is the typed outcome.

The owner keeps the durable rows, applies the one :class:`LivenessPolicy`, and
escalates a key the moment it parks. It never deletes work: parking only stops
the replanning path from trying again; whatever the action was protecting
(validated commits, a queued record) stays exactly where it is.

A park is released by progress, never by elapsed time alone:

* the facts change - a different fingerprint is a different key;
* the action succeeds under any fingerprint - :meth:`record` with ``done``
  clears every row of the identity, because the action demonstrably works;
* an operator retries or dismisses the escalation issue -
  :meth:`release_issue`, wired into the operator command.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..domain.action_liveness import (
    ActionOutcome,
    Admission,
    LivenessKey,
    LivenessPolicy,
    LivenessRow,
    admission,
)
from ..ports.action_liveness import ActionLivenessStore, LivenessEscalation

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class LivenessDecision:
    """The owner's answer to :meth:`ActionLivenessOwner.admit`."""

    admission: Admission
    row: LivenessRow | None

    @property
    def admitted(self) -> bool:
        return self.admission is Admission.ADMIT

    def describe(self) -> str:
        row = self.row
        if row is None or self.admission is Admission.ADMIT:
            return "admitted"
        if row.next_attempt_at is None:
            return f"parked after {row.last_outcome.value}: {row.last_reason}"
        return (
            f"backing off until {row.next_attempt_at.isoformat()} after"
            f" {row.attempts} failed attempt(s): {row.last_reason}"
        )


class ActionLivenessOwner:
    """Owns every durable liveness row and the one policy that writes them."""

    def __init__(
        self,
        *,
        store: ActionLivenessStore,
        escalation: LivenessEscalation,
        policy: LivenessPolicy = LivenessPolicy(),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._store = store
        self._escalation = escalation
        self._policy = policy
        self._clock = clock

    @property
    def policy(self) -> LivenessPolicy:
        return self._policy

    def admit(self, key: LivenessKey) -> LivenessDecision:
        row = self._store.row(key)
        return LivenessDecision(admission(row, self._clock()), row)

    def record(self, key: LivenessKey, outcome: ActionOutcome) -> LivenessRow | None:
        """Fold one attempt's outcome into the key's durable row.

        Returns the row left behind (``None`` after success). A row that parks
        on this call is escalated before returning; whether that committed is
        written back onto the row.
        """
        previous = self._store.row(key)
        row = self._policy.after(previous, key, outcome, self._clock())
        if row is None:
            self._resolve(self._store.clear_identity(key.identity))
            return None
        self._store.put(row)
        if not row.parked or (previous is not None and previous.parked):
            return row
        logger.warning(
            "[LIVENESS] Parked %s on %s (fingerprint %s): %s",
            key.identity.action,
            key.identity.subject,
            key.fingerprint,
            row.last_reason,
        )
        if self._escalation.escalate(row):
            row = replace(row, escalated=True)
            self._store.put(row)
        return row

    def release_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """An operator acted on ``issue_number``: every key it escalates gets a fresh budget.

        The operator command already settled the needs-human block itself, so
        this withdraws nothing from GitHub - it only lets the planner and the
        drain try again, and announces that on the timeline.
        """
        released = self._store.clear_escalation_issue(issue_number)
        if released:
            self._escalation.resolve(released, release_issue=False)
        return released

    def parked(self) -> tuple[LivenessRow, ...]:
        """Every parked row, for the tech-lead board and diagnostics."""
        return self._store.parked_rows()

    def _resolve(self, cleared: tuple[LivenessRow, ...]) -> None:
        parked = tuple(row for row in cleared if row.parked)
        if not parked:
            return
        issues = {
            row.key.escalation_issue
            for row in parked
            if row.escalated and row.key.escalation_issue is not None
        }
        still_held = {
            issue for issue in issues if self._store.escalated_rows_for_issue(issue)
        }
        released = tuple(row for row in parked if row.key.escalation_issue not in still_held)
        held = tuple(row for row in parked if row.key.escalation_issue in still_held)
        if released:
            self._escalation.resolve(released, release_issue=True)
        if held:
            self._escalation.resolve(held, release_issue=False)


__all__ = ["ActionLivenessOwner", "LivenessDecision"]
