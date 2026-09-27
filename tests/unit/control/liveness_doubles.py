"""Test doubles at the action liveness owner's port boundaries (#7350)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from issue_orchestrator.control.action_liveness import ActionLivenessOwner
from issue_orchestrator.control.planned_action_liveness import PlannedActionLiveness
from issue_orchestrator.control.planner_types import OrchestratorSnapshot, Plan
from issue_orchestrator.domain.owed_write import EffectDebt, EffectResult
from issue_orchestrator.ports.action_liveness import PendingPause, PendingRelease
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessPolicy,
    LivenessRow,
)


class InMemoryActionLivenessStore:
    """The ``ActionLivenessStore`` port, in dicts."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], LivenessRow] = {}
        self.releases: dict[int, PendingRelease] = {}
        self.pauses: dict[int, PendingPause] = {}
        self.announcements: dict = {}
        self._next_announcement = 0
        self.progress: dict[ActionIdentity, datetime] = {}

    @staticmethod
    def _id(key: LivenessKey) -> tuple[str, str, str]:
        return (key.identity.subject, key.identity.action, key.fingerprint)

    def row(self, key: LivenessKey) -> LivenessRow | None:
        return self.rows.get(self._id(key))

    def put(self, row: LivenessRow) -> None:
        self.rows[self._id(row.key)] = row

    def _pop(self, matches) -> tuple[LivenessRow, ...]:
        gone = tuple(row for row in self.rows.values() if matches(row))
        for row in gone:
            del self.rows[self._id(row.key)]
        return gone

    def _forget(self, matches) -> tuple[LivenessRow, ...]:
        from issue_orchestrator.domain.action_liveness import LivenessAnnouncement

        gone = self._pop(matches)
        for row in gone:
            if row.escalated and row.key.escalation_issue is not None:
                self.request_release(row.key.escalation_issue)
            if row.parked:
                self._owe(LivenessAnnouncement.RELEASED, row)
        return gone

    def clear_key(self, key: LivenessKey, *, done_at: datetime) -> tuple[LivenessRow, ...]:
        self.progress[key.identity] = done_at
        return self._forget(lambda row: row.key == key)

    def update_escalation(self, row: LivenessRow) -> bool:
        current = self.rows.get(self._id(row.key))
        if current is None or not current.parked or current.first_failed_at != row.first_failed_at:
            return False
        from dataclasses import replace

        self.rows[self._id(row.key)] = replace(
            current,
            escalated=row.escalated,
            explained=row.explained,
            escalation=row.escalation,
        )
        return True

    def release_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        return self._forget(lambda row: row.key.identity == identity)

    def _owe(self, kind, row: LivenessRow) -> None:
        self._next_announcement += 1
        self.announcements[self._next_announcement] = (kind, row)

    def settle(self, expected, row: LivenessRow, *, announce_parked: bool) -> bool:
        from issue_orchestrator.domain.action_liveness import LivenessAnnouncement

        current = self.row(row.key)
        if expected is None and current is not None:
            return False
        if expected is not None and (
            current is None
            or current.first_failed_at != expected.first_failed_at
            or current.attempts != expected.attempts
        ):
            return False
        self.put(row)
        if announce_parked:
            self._owe(LivenessAnnouncement.PARKED, row)
        return True

    def pending_announcements(self):
        return tuple(
            (number, kind, row) for number, (kind, row) in sorted(self.announcements.items())
        )

    def clear_announcement(self, announcement_id: int) -> None:
        del self.announcements[announcement_id]

    def retire_unplanned(
        self, *, abandoned_before: datetime, superseded_before: datetime
    ) -> tuple[LivenessRow, ...]:
        def retirable(row: LivenessRow) -> bool:
            done = self.progress.get(row.key.identity)
            superseded = done is not None and done > row.planned_at
            return row.planned_at < abandoned_before or (
                superseded and row.planned_at < superseded_before
            )

        return self._forget(retirable)

    def touch(self, key: LivenessKey, planned_at: datetime) -> None:
        from dataclasses import replace

        row = self.rows.get(self._id(key))
        if row is not None:
            self.rows[self._id(key)] = replace(row, last_planned_at=planned_at)

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        from issue_orchestrator.domain.action_liveness import LivenessAnnouncement

        self.releases.pop(issue_number, None)
        self.pauses.pop(issue_number, None)
        gone = self._pop(lambda row: row.key.escalation_issue == issue_number)
        for row in gone:
            if row.parked:
                self._owe(LivenessAnnouncement.RELEASED, row)
        return gone

    def parked_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return tuple(
            row
            for row in self.rows.values()
            if row.key.escalation_issue == issue_number and row.parked
        )

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        return tuple(sorted(
            (row for row in self.rows.values() if row.parked),
            key=lambda row: row.last_failed_at,
        ))

    def waiting_rows(self) -> tuple[LivenessRow, ...]:
        from issue_orchestrator.domain.action_liveness import OutcomeKind

        return tuple(
            row for row in self.rows.values()
            if not row.parked and row.last_outcome is OutcomeKind.WAITING
        )

    def rows_owing_escalation(self) -> tuple[LivenessRow, ...]:
        return tuple(row for row in self.parked_rows() if row.owes_escalation)

    def request_release(self, issue_number: int) -> None:
        self.releases.setdefault(issue_number, PendingRelease(issue_number))

    def pending_releases(self) -> tuple[PendingRelease, ...]:
        return tuple(self.releases[number] for number in sorted(self.releases))

    def set_release_debt(self, issue_number: int, debt: EffectDebt) -> None:
        if issue_number in self.releases:
            self.releases[issue_number] = PendingRelease(issue_number, debt)

    def request_pause(self, issue_number: int, reason: str) -> PendingPause:
        return self.pauses.setdefault(issue_number, PendingPause(issue_number, reason))

    def pending_pauses(self) -> tuple[PendingPause, ...]:
        return tuple(self.pauses[number] for number in sorted(self.pauses))

    def set_pause_debt(self, issue_number: int, debt: EffectDebt) -> None:
        if issue_number in self.pauses:
            pending = self.pauses[issue_number]
            self.pauses[issue_number] = PendingPause(issue_number, pending.reason, debt)

    def clear_pause(self, issue_number: int) -> None:
        self.pauses.pop(issue_number, None)

    def clear_release(self, issue_number: int) -> None:
        self.releases.pop(issue_number, None)

    def clear_release_if_escalated_park(self, issue_number: int) -> bool:
        if not any(park.escalated for park in self.parked_rows_for_issue(issue_number)):
            return False
        self.releases.pop(issue_number, None)
        return True


@dataclass
class RecordingEscalation:
    """The ``LivenessEscalation`` port, recording what it was asked to do.

    ``commits`` / ``unblock_commits`` decide whether the block / the release
    lands; flip them to model GitHub refusing and then recovering.
    """

    commits: bool = True
    explain_commits: bool = True
    unblock_commits: bool = True
    parked: list[LivenessRow] = field(default_factory=list)
    released: list[tuple[LivenessRow, ...]] = field(default_factory=list)
    blocks: list[tuple[LivenessRow, bool]] = field(default_factory=list)
    unblocks: list[tuple[int, bool]] = field(default_factory=list)
    explanations: list[tuple[LivenessRow, bool]] = field(default_factory=list)

    def announce_parked(self, row: LivenessRow) -> None:
        self.parked.append(row)

    def announce_released(self, rows: tuple[LivenessRow, ...]) -> None:
        self.released.append(rows)

    pause_commits: bool = True
    pauses: list[tuple[int, bool]] = field(default_factory=list)

    def block(self, row: LivenessRow) -> EffectResult:
        self.blocks.append((row, self.commits))
        return _result(self.commits)

    def explain(self, row: LivenessRow) -> EffectResult:
        self.explanations.append((row, self.explain_commits))
        return _result(self.explain_commits)

    def unblock(self, issue_number: int) -> EffectResult:
        self.unblocks.append((issue_number, self.unblock_commits))
        return _result(self.unblock_commits)

    def pause(self, issue_number: int, reason: str) -> EffectResult:
        self.pauses.append((issue_number, self.pause_commits))
        return _result(self.pause_commits)

    @property
    def committed_blocks(self) -> list[LivenessRow]:
        return [row for row, committed in self.blocks if committed]


def _result(committed: bool) -> EffectResult:
    return EffectResult.landed() if committed else EffectResult.refused("refused")


@dataclass
class ManualClock:
    now: datetime = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def liveness_owner(
    *,
    store: InMemoryActionLivenessStore | None = None,
    escalation: RecordingEscalation | None = None,
    clock: ManualClock | None = None,
    policy: LivenessPolicy = LivenessPolicy(),
) -> ActionLivenessOwner:
    return ActionLivenessOwner(
        store=store if store is not None else InMemoryActionLivenessStore(),
        escalation=escalation if escalation is not None else RecordingEscalation(),
        policy=policy,
        clock=clock if clock is not None else ManualClock(),
    )


def gated(plan: Plan, owner: ActionLivenessOwner | None = None) -> Plan:
    """Admit ``plan`` through a liveness gate, as ``run_planning_cycle`` does."""
    snapshot = OrchestratorSnapshot(
        issues=(),
        active_sessions=(),
        pending_reviews=(),
        pending_reworks=(),
        pending_tech_lead=(),
        paused=False,
    )
    return PlannedActionLiveness(
        owner or liveness_owner(), escalation_label="needs-human"
    ).admit(plan, snapshot)


class _PassthroughLiveness:
    """A gate for tests about FETCHING, whose planner and plan are mocks."""

    def admit(self, plan, snapshot):
        return plan


PASSTHROUGH_LIVENESS = _PassthroughLiveness()


class QueuedOnIssueOne:
    """Record facts for drain tests that do not look at them: every record is
    queued on issue 1 with no attached evidence."""

    def get(self, _record_id: str):
        from types import SimpleNamespace

        from issue_orchestrator.domain.validated_work import ValidatedWorkState

        return SimpleNamespace(key=SimpleNamespace(issue_number=1), state=ValidatedWorkState.QUEUED)

    def attached_evidence(self, _record_id: str) -> tuple:
        return ()


def drain_liveness(owner: ActionLivenessOwner | None = None, *, records=None):
    """A recovery-drain liveness over an in-memory owner (#7350); ``records``
    is the validated-work store (or a stand-in for its two fact reads)."""
    from issue_orchestrator.control.recovery_drain_liveness import RecoveryDrainLiveness

    return RecoveryDrainLiveness(
        owner=owner or liveness_owner(),
        records=records if records is not None else QueuedOnIssueOne(),
    )


def applier_owner(applier, events, *, store=None, clock=None, policy=LivenessPolicy()):
    """A liveness owner whose owed writes go through a real
    ``ActionLivenessEscalation`` over ``applier`` (#7350)."""
    from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation

    return ActionLivenessOwner(
        store=store if store is not None else InMemoryActionLivenessStore(),
        escalation=ActionLivenessEscalation(
            events=events, applier=applier, needs_human_label="needs-human"
        ),
        policy=policy,
        clock=clock if clock is not None else ManualClock(),
    )
