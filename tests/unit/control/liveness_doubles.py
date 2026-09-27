"""Test doubles at the action liveness owner's port boundaries (#7350)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from issue_orchestrator.control.action_liveness import ActionLivenessOwner
from issue_orchestrator.control.planned_action_liveness import PlannedActionLiveness
from issue_orchestrator.control.planner_types import OrchestratorSnapshot, Plan
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessPolicy,
    LivenessRow,
)


class InMemoryActionLivenessStore:
    """The ``ActionLivenessStore`` port, in a dict."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], LivenessRow] = {}

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

    def clear_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        return self._pop(lambda row: row.key.identity == identity)

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return self._pop(lambda row: row.key.escalation_issue == issue_number)

    def escalated_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        return tuple(
            row
            for row in self.rows.values()
            if row.key.escalation_issue == issue_number and row.escalated and row.parked
        )

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        return tuple(sorted(
            (row for row in self.rows.values() if row.parked),
            key=lambda row: row.last_failed_at,
        ))


@dataclass
class RecordingEscalation:
    """The ``LivenessEscalation`` port, recording what it was asked to do."""

    commits: bool = True
    escalated: list[LivenessRow] = field(default_factory=list)
    resolved: list[tuple[tuple[LivenessRow, ...], bool]] = field(default_factory=list)

    def escalate(self, row: LivenessRow) -> bool:
        self.escalated.append(row)
        return self.commits and row.key.escalation_issue is not None

    def resolve(self, rows: tuple[LivenessRow, ...], *, release_issue: bool) -> None:
        self.resolved.append((rows, release_issue))


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
    return PlannedActionLiveness(owner or liveness_owner()).admit(plan, snapshot)


class _PassthroughLiveness:
    """A gate for tests about FETCHING, whose planner and plan are mocks."""

    def admit(self, plan, snapshot):
        return plan


PASSTHROUGH_LIVENESS = _PassthroughLiveness()
