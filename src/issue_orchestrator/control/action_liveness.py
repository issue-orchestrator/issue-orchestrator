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
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from ..domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    Admission,
    LivenessAnnouncement,
    LivenessKey,
    LivenessPolicy,
    LivenessRow,
    admission,
)
from ..ports.action_liveness import ActionLivenessStore, LivenessEscalation
from ..ports.blocked_item_custody import ParkedActionFact

logger = logging.getLogger(__name__)


def _action_kind(operation: str) -> str:
    """The action kind an operation names, without its label, signature or
    fact prefix (``add_label:needs-human`` -> ``add_label``)."""
    for separator in (":", "#", ">"):
        operation = operation.split(separator, 1)[0]
    return operation


def release_parked_action(
    store: ActionLivenessStore, identity: ActionIdentity
) -> tuple[LivenessRow, ...]:
    """The one operator release of an action, in-process or from the CLI.

    One store transaction forgets the rows, owes their blocks' release and owes
    their ``action.released`` announcement; the engine's next
    :meth:`ActionLivenessOwner.reconcile_effects` settles both.
    """
    return store.release_identity(identity)


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
        # Escalation and withdrawal are decided and written one at a time: the
        # planner's tick, the recovery drain's worker and operator commands
        # all reach this owner, and a withdrawal that read "no park" must not
        # take off a block another thread lands meanwhile.
        self._effects = threading.RLock()

    @property
    def policy(self) -> LivenessPolicy:
        return self._policy

    def admit(self, key: LivenessKey) -> LivenessDecision:
        """May ``key`` run now? Asking also marks its row as still a live question."""
        row = self._store.row(key)
        now = self._clock()
        if row is not None:
            self._store.touch(key, now)
        return LivenessDecision(admission(row, now), row)

    def record(self, key: LivenessKey, outcome: ActionOutcome) -> LivenessRow | None:
        """Fold one attempt's outcome into the key's durable row.

        Success clears exactly this key. Rows under OTHER fingerprints of the
        same identity may be older facts of this operation or a different
        operation on the same subject (two comments on one issue), and nothing
        here can tell them apart; so they are left to :meth:`reconcile_effects`,
        which retires any row no path has asked about for ``stale_after``.

        Returns the row left behind (``None`` after success). A row that parks
        on this call is announced and its block attempted before returning;
        a block that does not commit is retried by :meth:`reconcile_effects`.
        """
        previous = self._store.row(key)
        now = self._clock()
        row = self._policy.after(previous, key, outcome, now)
        if row is None:
            self._resolve(self._store.clear_key(key, done_at=self._clock()))
            return None
        newly_parked = row.parked and not (previous is not None and previous.parked)
        if not self._store.settle(previous, row, announce_parked=newly_parked):
            # An operator released this key while the attempt ran: the
            # release stands and this stale settlement is discarded.
            return None
        if not newly_parked:
            return row
        self._publish_announcements()
        logger.warning(
            "[LIVENESS] Parked %s on %s (fingerprint %s): %s",
            key.identity.action,
            key.identity.subject,
            key.fingerprint,
            row.last_reason,
        )
        return self._escalate(row, now)

    def reconcile_effects(self) -> None:
        """Retry every escalation effect that has not committed yet.

        A park whose block did not land, and a release owed to an issue whose
        withdrawal did not land, are both durable here; each is retried at the
        policy's pace and bounded by its budget, so a label GitHub keeps
        refusing is neither lost nor hammered. Called once per planning cycle.
        """
        now = self._clock()
        self._publish_announcements()
        # A question nobody asks any more is not parked: its facts changed or
        # the action is no longer wanted. Retiring it owes its block's release.
        self._resolve(
            self._store.retire_unplanned(
                abandoned_before=now - self._policy.abandon_after,
                superseded_before=now - self._policy.stale_after,
            )
        )
        for row in self._store.rows_owing_escalation():
            if self._policy.effect_due(row.escalation_attempts, row.escalation_attempted_at, now):
                self._escalate(row, now)
        for pending in self._store.pending_releases():
            if self._policy.effect_due(pending.attempts, pending.attempted_at, now):
                self._unblock(pending.issue_number, now)

    def release_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """``issue_number`` was settled by a person or by terminal recovery:
        every key it escalates gets a fresh budget.

        An operator's Retry or Dismiss, or a terminal recovery, already settled
        the needs-human block itself, so
        this withdraws nothing from GitHub and forgets any withdrawal still
        owed - it only lets the planner and the drain try again, and announces
        that on the timeline.
        """
        released = self._store.clear_escalation_issue(issue_number)
        self._publish_announcements()
        return released

    def release_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        """An operator releases one action on one subject, whatever its facts.

        The route for a park no issue carries (an ``engine`` subject), or one
        a person wants retried without touching its issue. Blocks the released
        rows escalated are owed their release, which :meth:`reconcile_effects`
        settles, as it publishes the release on the timeline.
        """
        return release_parked_action(self._store, identity)

    def parked(self) -> tuple[LivenessRow, ...]:
        """Every parked row, for the tech-lead board and diagnostics."""
        return self._store.parked_rows()

    def parked_for_issue(self, issue_number: int) -> tuple[ParkedActionFact, ...]:
        """What this owner holds on ``issue_number``: blocked-item custody's
        "Held" (the :class:`~..ports.blocked_item_custody.ParkedActionReader`
        seam, #7331). A park escalated on the issue holds it, block landed or
        not, since each is this owner's decision to stop retrying."""
        return tuple(
            ParkedActionFact(
                action=_action_kind(row.key.identity.action),
                outcome=row.last_outcome.value,
                reason=row.last_reason,
                parked_since=row.last_failed_at,
            )
            for row in self._store.parked_rows_for_issue(issue_number)
        )

    def _publish_announcements(self) -> None:
        """Publish every owed announcement, then acknowledge it (at least once)."""
        for announcement_id, kind, row in self._store.pending_announcements():
            if kind is LivenessAnnouncement.PARKED:
                self._escalation.announce_parked(row)
            else:
                self._escalation.announce_released((row,))
            self._store.clear_announcement(announcement_id)

    def _escalate(self, row: LivenessRow, now: datetime) -> LivenessRow:
        """Land whichever escalation effect is still owed: the block, then its comment.

        Each is a separate durable debt. The comment is owed only once the block
        has landed, and its attempts are paced from that moment.
        """
        with self._effects:
            return self._escalate_serialized(row, now)

    def _escalate_serialized(self, row: LivenessRow, now: datetime) -> LivenessRow:
        issue = row.key.escalation_issue
        if issue is None:
            return row
        if not row.escalated:
            if not self._escalation.block(row):
                return self._put_attempt(row, now)
            row = replace(row, escalated=True, escalation_attempts=0, escalation_attempted_at=None)
            if not self._store.update_escalation(row):
                # Released while the block was being written: the park is gone,
                # so the block that just landed is owed its withdrawal.
                self._store.request_release(issue)
                self._unblock(issue, now)
                return row
        if not self._escalation.explain(row):
            return self._put_attempt(row, now)
        row = replace(row, explained=True, escalation_attempts=0, escalation_attempted_at=None)
        self._store.update_escalation(row)
        return row

    def _put_attempt(self, row: LivenessRow, now: datetime) -> LivenessRow:
        row = replace(
            row,
            escalation_attempts=row.escalation_attempts + 1,
            escalation_attempted_at=now,
        )
        self._store.update_escalation(row)
        return row

    def _resolve(self, cleared: tuple[LivenessRow, ...]) -> None:
        """Announce parks that ended, and settle each freed issue's owed release."""
        self._publish_announcements()
        parked = tuple(row for row in cleared if row.parked)
        if not parked:
            return
        now = self._clock()
        # The store's clear already owed each freed issue its release in the
        # same transaction that forgot the parks; try to settle them now.
        for issue in sorted(
            {
                row.key.escalation_issue
                for row in parked
                if row.escalated and row.key.escalation_issue is not None
            }
        ):
            self._unblock(issue, now)

    def _unblock(self, issue_number: int, now: datetime) -> None:
        with self._effects:
            self._unblock_serialized(issue_number, now)

    def _unblock_serialized(self, issue_number: int, now: datetime) -> None:
        if self._store.clear_release_if_escalated_park(issue_number):
            # Another park's committed block is the one on the issue now.
            return
        if self._store.parked_rows_for_issue(issue_number):
            # A park still stands whose own block has not landed: the label on
            # the issue is its block too. Keep the debt; it settles once that
            # park lands its block (above) or is itself gone (below).
            return
        if self._escalation.unblock(issue_number):
            self._store.clear_release(issue_number)
            return
        self._store.record_release_attempt(issue_number, now)


__all__ = ["ActionLivenessOwner", "LivenessDecision", "release_parked_action"]
