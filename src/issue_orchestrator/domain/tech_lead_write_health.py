"""Is the tech lead still WRITING, or only thinking? (#7080)

Between 2026-08-07 and 2026-08-17 the tech-lead subsystem produced no decision
action that reached GitHub while launches continued at full rate: 20
``tech_lead.run_requested`` rows in the window, a newest ``action_proposed`` of
2026-08-07T13:33Z, and not one ``tech_lead.action_executed`` row in the store at
all. Nothing detected it. A diagnosis subsystem whose output never lands is worse
than one that is switched off, because it keeps consuming agent capacity and
manufacturing `blocked-failed` labels and re-investigation queue entries as a
side effect.

The signal is a comparison of durable event recency, and the distinction that
matters is between two very different silences:

* **proposals stopped too** — the runs are not producing decisions at all;
* **proposals continue but nothing executes** — the runs ARE deciding and the
  decisions are stuck behind an approval gate or an applier.

Collapsing those into one "stale" boolean sends an operator to the wrong half of
the system, so :class:`TechLeadWriteVerdict` keeps them apart. This module
decides only; the facts are read by a caller and ``now`` is always injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

#: Events proving a tech-lead run was ASKED FOR.
RUN_REQUESTED_EVENTS: tuple[str, ...] = ("tech_lead.run_requested",)

#: Events proving a run produced a decision, whether or not it was applied.
DECISION_PROPOSED_EVENTS: tuple[str, ...] = (
    "tech_lead.action_proposed",
    "tech_lead.decision_rejected",
)

#: Events proving a tech-lead decision reached GitHub.
#:
#: Deliberately NOT ``tech_lead.issue_created``: that event fires for
#: health-review ANCHOR minting as well as for decisions (10 anchors against 5
#: decisions among the health-review-flavoured rows in the live store), and
#: #7080 records that reading an interval anchor as a decision is exactly how
#: the ten-day silence stayed invisible. A decision-driven creation now emits
#: ``tech_lead.action_executed`` of its own
#: (``control.tech_lead_issue_creation``), so this set needs no payload filter.
DECISION_EXECUTED_EVENTS: tuple[str, ...] = ("tech_lead.action_executed",)

#: Every event name this assessment reads.
WRITE_HEALTH_EVENTS: tuple[str, ...] = (
    *RUN_REQUESTED_EVENTS,
    *DECISION_PROPOSED_EVENTS,
    *DECISION_EXECUTED_EVENTS,
)


class TechLeadWriteVerdict(str, Enum):
    """What the recency comparison says about the subsystem."""

    #: A decision reached GitHub inside the window. Nothing to report.
    WRITING = "writing"
    #: No run was requested inside the window, so silence proves nothing.
    IDLE = "idle"
    #: Runs continue and produce decisions, but none has been applied inside
    #: the window. The runs are working; the applier or its approval gate is
    #: where to look.
    PROPOSING_ONLY = "proposing_only"
    #: Runs continue and have produced neither a proposal nor an execution
    #: inside the window. The runs themselves are not reaching a decision.
    SILENT = "silent"

    @property
    def is_alarm(self) -> bool:
        """True when an operator or health review must act on this."""
        return self in {
            TechLeadWriteVerdict.PROPOSING_ONLY,
            TechLeadWriteVerdict.SILENT,
        }


@dataclass(frozen=True)
class TechLeadWriteActivity:
    """Newest durable timestamp for each class of tech-lead event.

    ``None`` means the store holds no such event at all, which is a real and
    distinct answer from "old": a subsystem that has NEVER executed an action is
    not the same as one that has stopped.
    """

    last_run_requested_at: datetime | None = None
    last_decision_proposed_at: datetime | None = None
    last_decision_executed_at: datetime | None = None


@dataclass(frozen=True)
class TechLeadWriteHealth:
    """The verdict plus the evidence it was reached from."""

    verdict: TechLeadWriteVerdict
    stale_after_hours: float
    reason: str
    run_requested_age_hours: float | None
    decision_proposed_age_hours: float | None
    decision_executed_age_hours: float | None

    @property
    def is_alarm(self) -> bool:
        return self.verdict.is_alarm


def _age_hours(now: datetime, moment: datetime | None) -> float | None:
    if moment is None:
        return None
    return round((now - moment).total_seconds() / 3600.0, 2)


def _within(age_hours: float | None, window_hours: float) -> bool:
    return age_hours is not None and age_hours <= window_hours


def assess_tech_lead_write_health(
    activity: TechLeadWriteActivity,
    *,
    now: datetime,
    stale_after_hours: float,
) -> TechLeadWriteHealth:
    """Classify tech-lead write activity against one window.

    The window is anchored on ``run_requested`` deliberately. Judging silence
    against the wall clock alone would alarm on a deliberately idle engine and
    stay quiet on a busy one whose writes had stopped -- the exact inversion of
    what #7080 needs.
    """
    if stale_after_hours <= 0:
        raise ValueError(
            f"stale_after_hours must be positive, got {stale_after_hours!r}"
        )

    requested_age = _age_hours(now, activity.last_run_requested_at)
    proposed_age = _age_hours(now, activity.last_decision_proposed_at)
    executed_age = _age_hours(now, activity.last_decision_executed_at)

    def health(verdict: TechLeadWriteVerdict, reason: str) -> TechLeadWriteHealth:
        return TechLeadWriteHealth(
            verdict=verdict,
            stale_after_hours=stale_after_hours,
            reason=reason,
            run_requested_age_hours=requested_age,
            decision_proposed_age_hours=proposed_age,
            decision_executed_age_hours=executed_age,
        )

    if _within(executed_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.WRITING,
            f"a tech-lead decision reached GitHub {executed_age}h ago",
        )
    if not _within(requested_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.IDLE,
            "no tech-lead run was requested inside the window;"
            " silence is expected",
        )
    never = "no tech-lead decision has EVER been applied"
    applied = (
        never
        if executed_age is None
        else f"the newest applied decision is {executed_age}h old"
    )
    if _within(proposed_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.PROPOSING_ONLY,
            f"runs are proposing decisions ({proposed_age}h ago) but {applied};"
            " look at the approval gate and the act-level appliers, not the runs",
        )
    proposed = (
        "no tech-lead decision has EVER been proposed"
        if proposed_age is None
        else f"the newest proposal is {proposed_age}h old"
    )
    return health(
        TechLeadWriteVerdict.SILENT,
        f"runs continue ({requested_age}h ago) but {proposed} and {applied}",
    )
