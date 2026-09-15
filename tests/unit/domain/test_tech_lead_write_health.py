"""Tech-lead write-health classification (#7080).

The numbers are the measured ones from the incident: between 2026-08-07 and
2026-08-17 the store held 20 `tech_lead.run_requested` rows, a newest
`action_proposed` of 2026-08-07T13:33Z, and no `action_executed` row at all.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.domain.tech_lead_write_health import (
    DECISION_EXECUTED_EVENTS,
    DECISION_PROPOSED_EVENTS,
    TechLeadWriteActivity,
    TechLeadWriteVerdict,
    assess_tech_lead_write_health,
    unavailable,
)

NOW = datetime(2026, 8, 17, 7, 32, tzinfo=timezone.utc)
WINDOW = 48.0
#: The subsystem has plainly been up long enough to be judged.
LONG_RUNNING = NOW - timedelta(days=30)


def _assess(*, enabled: bool = True, **kwargs: datetime | None):
    return assess_tech_lead_write_health(
        TechLeadWriteActivity(**kwargs),
        now=NOW,
        stale_after_hours=WINDOW,
        tech_lead_enabled=enabled,
    )


class TestTheIncident:
    def test_ten_day_silence_with_runs_continuing_is_an_alarm(self) -> None:
        health = _assess(
            first_run_requested_at=datetime(2026, 8, 7, 13, 0, tzinfo=timezone.utc),
            last_run_requested_at=NOW - timedelta(minutes=1),
            last_decision_proposed_at=datetime(2026, 8, 7, 13, 33, tzinfo=timezone.utc),
            last_decision_executed_at=None,
        )

        assert health.verdict is TechLeadWriteVerdict.SILENT
        assert health.is_alarm
        assert "no tech-lead decision has EVER been applied" in health.reason
        assert health.silent_for_hours is not None
        assert health.silent_for_hours > 48

    def test_proposals_continuing_without_execution_names_the_approval_gate(
        self,
    ) -> None:
        # The measured cause: reset_retry/kill_hung_session default to
        # `propose`, so decisions become gated issues nobody approves.
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(hours=1),
            last_decision_proposed_at=NOW - timedelta(hours=2),
            last_decision_executed_at=NOW - timedelta(days=30),
        )

        assert health.verdict is TechLeadWriteVerdict.PROPOSING_ONLY
        assert health.is_alarm
        assert "approval gate" in health.reason
        # The diagnosis must send the reader to the appliers, not the runs.
        assert "not the runs" in health.reason


class TestTheWindowIsAGracePeriodNotALookback:
    """Silence must have PERSISTED for the window (#7262 review F1)."""

    def test_a_brand_new_engines_first_request_does_not_alarm(self) -> None:
        # One request seconds ago on an empty store is not write-death; the
        # subsystem has not had time to write anything.
        health = _assess(
            first_run_requested_at=NOW - timedelta(minutes=1),
            last_run_requested_at=NOW - timedelta(minutes=1),
        )

        assert not health.is_alarm
        assert health.verdict is TechLeadWriteVerdict.WRITING

    def test_silence_is_measured_from_the_last_decision_not_the_first_run(
        self,
    ) -> None:
        # A 30-day-old engine that wrote 20h ago has been silent for 20h, not
        # for 30 days: the clock restarts at each landed decision.
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW,
            last_decision_executed_at=NOW - timedelta(hours=20),
        )

        assert health.verdict is TechLeadWriteVerdict.WRITING
        assert health.decision_executed_age_hours == pytest.approx(20.0)

    def test_the_alarm_fires_once_the_silence_outlives_the_window(self) -> None:
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW,
            last_decision_executed_at=NOW - timedelta(hours=60),
            last_decision_proposed_at=NOW - timedelta(hours=1),
        )

        assert health.verdict is TechLeadWriteVerdict.PROPOSING_ONLY


class TestHealthyIdleAndDisabled:
    def test_a_recent_applied_decision_is_healthy(self) -> None:
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(hours=1),
            last_decision_proposed_at=NOW - timedelta(hours=1),
            last_decision_executed_at=NOW - timedelta(hours=1),
        )

        assert health.verdict is TechLeadWriteVerdict.WRITING
        assert not health.is_alarm

    def test_an_engine_with_no_recent_runs_is_idle_not_stale(self) -> None:
        # Judging silence against the wall clock alone would alarm here, on an
        # engine that simply is not running the tech lead.
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(days=30),
            last_decision_proposed_at=NOW - timedelta(days=30),
            last_decision_executed_at=NOW - timedelta(days=30),
        )

        assert health.verdict is TechLeadWriteVerdict.IDLE
        assert not health.is_alarm

    def test_a_disabled_tech_lead_never_alarms(self) -> None:
        # `run_requested` fires for REJECTED requests too, including on a
        # disabled engine, so recency alone would alarm on a switched-off tech
        # lead (#7262 review F1).
        health = _assess(
            enabled=False,
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW,
        )

        assert health.verdict is TechLeadWriteVerdict.IDLE
        assert "not enabled" in health.reason

    def test_a_store_with_no_tech_lead_history_at_all_is_idle(self) -> None:
        assert _assess().verdict is TechLeadWriteVerdict.IDLE

    def test_an_old_run_with_a_fresh_decision_is_still_writing(self) -> None:
        # Execution recency wins: a decision landing proves the path works even
        # if no run has been requested lately.
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(days=10),
            last_decision_executed_at=NOW - timedelta(hours=2),
        )

        assert health.verdict is TechLeadWriteVerdict.WRITING


class TestWindowBoundary:
    @pytest.mark.parametrize(
        ("executed_hours_ago", "expected"),
        [
            (WINDOW - 0.01, TechLeadWriteVerdict.WRITING),
            (WINDOW, TechLeadWriteVerdict.WRITING),
            (WINDOW + 0.01, TechLeadWriteVerdict.SILENT),
        ],
    )
    def test_the_window_is_inclusive(
        self, executed_hours_ago: float, expected: TechLeadWriteVerdict
    ) -> None:
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(minutes=1),
            last_decision_executed_at=NOW - timedelta(hours=executed_hours_ago),
        )

        assert health.verdict is expected


class TestUnavailableIsReportedNotErased:
    """A health signal that quietly disappears is the failure mode (#7262 F5)."""

    def test_unavailable_is_an_alarm(self) -> None:
        health = unavailable(WINDOW, "the timeline database is locked")

        assert health.verdict is TechLeadWriteVerdict.UNAVAILABLE
        assert health.is_alarm
        assert "locked" in health.reason


class TestEventSelection:
    def test_a_rejected_decision_is_not_a_proposal(self) -> None:
        # `tech_lead.decision_rejected` is the orchestrator REFUSING a missing
        # or malformed artifact, with no GitHub call at all. Counting it would
        # report `proposing_only` and send the reader to the approval gate for a
        # subsystem whose runs are failing to decide (#7262 review F4).
        assert "tech_lead.decision_rejected" not in DECISION_PROPOSED_EVENTS

    def test_anchor_minting_is_not_an_executed_decision(self) -> None:
        # `tech_lead.issue_created` fires for health-review anchors too.
        assert "tech_lead.issue_created" not in DECISION_EXECUTED_EVENTS


class TestEvidenceIsCarried:
    def test_every_age_is_reported_alongside_the_verdict(self) -> None:
        health = _assess(
            first_run_requested_at=LONG_RUNNING,
            last_run_requested_at=NOW - timedelta(hours=1),
            last_decision_proposed_at=NOW - timedelta(hours=2),
            last_decision_executed_at=NOW - timedelta(hours=100),
        )

        assert health.run_requested_age_hours == 1.0
        assert health.decision_proposed_age_hours == 2.0
        assert health.decision_executed_age_hours == 100.0
        assert health.stale_after_hours == WINDOW

    @pytest.mark.parametrize(
        "window", [0, -1, math.inf, -math.inf, math.nan]
    )
    def test_a_window_that_disables_the_alarm_is_rejected(self, window: float) -> None:
        # NaN makes every comparison False (a busy engine reads as idle); inf
        # makes any historical execution count as writing forever (#7262 F8).
        with pytest.raises(ValueError, match="positive, finite"):
            assess_tech_lead_write_health(
                TechLeadWriteActivity(), now=NOW, stale_after_hours=window
            )
