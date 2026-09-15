"""Is the tech lead still WRITING, or only thinking? (#7080)

Between 2026-08-07 and 2026-08-17 the tech-lead subsystem produced no decision
action that reached GitHub while launches continued at full rate: 20
``tech_lead.run_requested`` rows in the window, a newest ``action_proposed`` of
2026-08-07T13:33Z, and not one ``tech_lead.action_executed`` row in the store at
all. Nothing detected it. A diagnosis subsystem whose output never lands is worse
than one that is switched off, because it keeps consuming agent capacity and
manufacturing `blocked-failed` labels and re-investigation queue entries as a
side effect.

The signal is a comparison of durable event recency, and two distinctions carry
all of its value.

**Silence that has PERSISTED, not silence observed once.** The window is a
grace period, not a lookback. An engine that requested its first run a minute ago
and has not written yet is not write-dead -- it has not had time to be. So the
measurement is *how long the subsystem has been requesting runs without writing*:
the elapsed time since the later of "the first run we have evidence of" and "the
last decision that landed". On a fresh store that is seconds; across the real
incident it was ten days (#7262 review F1).

**Proposing-but-not-applying, kept apart from producing nothing.** They send an
operator to opposite halves of the system: to the approval gate and the appliers,
or to the runs themselves. Collapsing them into one "stale" boolean loses exactly
the information that makes the alarm actionable.

This module decides only; the facts are read by a caller and ``now`` is always
injected.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

#: Events proving a tech-lead run was ASKED FOR.
#:
#: This fires for REJECTED requests too, including on a disabled engine, so it
#: is evidence that something is asking -- never that a run ran. The assessment
#: therefore treats it as a liveness bound and takes "is the tech lead even
#: enabled" as a separate, configured input (#7262 review F1).
RUN_REQUESTED_EVENTS: tuple[str, ...] = ("tech_lead.run_requested",)

#: Events proving a run produced a decision, whether or not it was applied.
#:
#: Deliberately NOT ``tech_lead.decision_rejected``: that is the orchestrator
#: REFUSING a missing or malformed decision artifact, with no GitHub call at all.
#: Counting it as a proposal reports ``proposing_only`` -- "the runs are
#: deciding, look at the approval gate" -- for a subsystem whose runs are in fact
#: failing to produce a usable decision, which sends the reader to the wrong
#: place entirely (#7262 review F4).
DECISION_PROPOSED_EVENTS: tuple[str, ...] = ("tech_lead.action_proposed",)

#: Events proving a tech-lead decision reached GitHub.
#:
#: Deliberately NOT ``tech_lead.issue_created``: that event fires for
#: health-review ANCHOR minting as well as for decisions (10 anchors against 5
#: decisions among the health-review-flavoured rows in the live store), and
#: reading an interval anchor as a decision is exactly how the ten-day silence
#: stayed invisible. Every applied decision effect emits
#: ``tech_lead.action_executed`` through one receipt owner
#: (``control.tech_lead_decision_receipt``), so this set needs no payload filter.
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
    #: The tech lead is switched off, or no run was requested inside the window.
    #: Silence proves nothing.
    IDLE = "idle"
    #: Runs continue and produce decisions, but none has been applied for longer
    #: than the window. The runs are working; the applier or its approval gate is
    #: where to look.
    PROPOSING_ONLY = "proposing_only"
    #: Runs continue and have produced neither a proposal nor an execution for
    #: longer than the window. The runs themselves are not reaching a decision.
    SILENT = "silent"
    #: The durable evidence could not be read. Reported rather than hidden: a
    #: health signal that silently disappears is the failure mode this module
    #: exists to stop (#7262 review F5).
    UNAVAILABLE = "unavailable"

    @property
    def is_alarm(self) -> bool:
        """True when an operator or health review must act on this."""
        return self in {
            TechLeadWriteVerdict.PROPOSING_ONLY,
            TechLeadWriteVerdict.SILENT,
            TechLeadWriteVerdict.UNAVAILABLE,
        }


@dataclass(frozen=True)
class TechLeadWriteActivity:
    """Durable time bounds for each class of tech-lead event.

    ``None`` means the store holds no such event at all, which is a real and
    distinct answer from "old": a subsystem that has NEVER executed an action is
    not the same as one that has stopped.

    ``first_run_requested_at`` is what makes the window a grace period. Note that
    the trace store TRIMS old records, so it can only move FORWARD over time --
    which shortens the measured silence and makes the assessment under-report
    rather than raise a false alarm (#7262 review F7).
    """

    first_run_requested_at: datetime | None = None
    last_run_requested_at: datetime | None = None
    last_decision_proposed_at: datetime | None = None
    last_decision_executed_at: datetime | None = None


@dataclass(frozen=True)
class TechLeadWriteHealth:
    """The verdict plus the evidence it was reached from."""

    verdict: TechLeadWriteVerdict
    stale_after_hours: float
    reason: str
    silent_for_hours: float | None = None
    run_requested_age_hours: float | None = None
    decision_proposed_age_hours: float | None = None
    decision_executed_age_hours: float | None = None

    @property
    def is_alarm(self) -> bool:
        return self.verdict.is_alarm


def unavailable(stale_after_hours: float, reason: str) -> TechLeadWriteHealth:
    """The verdict for evidence that could not be read."""
    return TechLeadWriteHealth(
        verdict=TechLeadWriteVerdict.UNAVAILABLE,
        stale_after_hours=stale_after_hours,
        reason=reason,
    )


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
    tech_lead_enabled: bool = True,
) -> TechLeadWriteHealth:
    """Classify tech-lead write activity against one window.

    The window is anchored on run activity deliberately. Judging silence against
    the wall clock alone would alarm on a deliberately idle engine and stay quiet
    on a busy one whose writes had stopped -- the exact inversion of what #7080
    needs.
    """
    if not math.isfinite(stale_after_hours) or stale_after_hours <= 0:
        # A NaN window makes every comparison False (a busy engine reads as
        # idle); an infinite one makes any historical execution count as
        # `writing` forever. Both silently disable the alarm (#7262 review F8).
        raise ValueError(
            "stale_after_hours must be a positive, finite number of hours, got"
            f" {stale_after_hours!r}"
        )

    requested_age = _age_hours(now, activity.last_run_requested_at)
    proposed_age = _age_hours(now, activity.last_decision_proposed_at)
    executed_age = _age_hours(now, activity.last_decision_executed_at)

    def health(
        verdict: TechLeadWriteVerdict,
        reason: str,
        *,
        silent_for: float | None = None,
    ) -> TechLeadWriteHealth:
        return TechLeadWriteHealth(
            verdict=verdict,
            stale_after_hours=stale_after_hours,
            reason=reason,
            silent_for_hours=silent_for,
            run_requested_age_hours=requested_age,
            decision_proposed_age_hours=proposed_age,
            decision_executed_age_hours=executed_age,
        )

    if not tech_lead_enabled:
        return health(
            TechLeadWriteVerdict.IDLE,
            "the tech lead is not enabled for this repository",
        )
    if _within(executed_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.WRITING,
            f"a tech-lead decision reached GitHub {executed_age}h ago",
        )
    if not _within(requested_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.IDLE,
            "no tech-lead run was requested inside the window; silence is"
            " expected",
        )

    # How long the subsystem has been asking for runs WITHOUT writing: from the
    # later of the last applied decision and the first run we have evidence of.
    # A brand-new engine has been silent for seconds, not for the window.
    since = activity.last_decision_executed_at
    if since is None or (
        activity.first_run_requested_at is not None
        and activity.first_run_requested_at > since
    ):
        since = activity.first_run_requested_at
    silent_for = _age_hours(now, since)
    if silent_for is None or silent_for <= stale_after_hours:
        return health(
            TechLeadWriteVerdict.WRITING,
            "the tech lead has not been running long enough without writing to"
            f" call it silent ({silent_for}h of a {stale_after_hours}h window)",
            silent_for=silent_for,
        )

    applied = (
        "no tech-lead decision has EVER been applied"
        if executed_age is None
        else f"the newest applied decision is {executed_age}h old"
    )
    if _within(proposed_age, stale_after_hours):
        return health(
            TechLeadWriteVerdict.PROPOSING_ONLY,
            f"runs have been asking for {silent_for}h and are still proposing"
            f" decisions ({proposed_age}h ago), but {applied}; look at the"
            " approval gate and the act-level appliers, not the runs",
            silent_for=silent_for,
        )
    proposed = (
        "no tech-lead decision has EVER been proposed"
        if proposed_age is None
        else f"the newest proposal is {proposed_age}h old"
    )
    return health(
        TechLeadWriteVerdict.SILENT,
        f"runs have been asking for {silent_for}h ({requested_age}h since the"
        f" last) but {proposed} and {applied}",
        silent_for=silent_for,
    )
