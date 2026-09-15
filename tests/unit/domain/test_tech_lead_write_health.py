"""Tech-lead write-health classification (#7080).

The numbers in these tests are the measured ones from the incident: between
2026-08-07 and 2026-08-17 the store held 20 `tech_lead.run_requested` rows, a
newest `action_proposed` of 2026-08-07T13:33Z, and no `action_executed` row at
all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from issue_orchestrator.domain.tech_lead_write_health import (
    TechLeadWriteActivity,
    TechLeadWriteVerdict,
    assess_tech_lead_write_health,
)

NOW = datetime(2026, 8, 17, 7, 32, tzinfo=timezone.utc)
WINDOW = 48.0


def _assess(**kwargs: datetime | None):
    return assess_tech_lead_write_health(
        TechLeadWriteActivity(**kwargs), now=NOW, stale_after_hours=WINDOW
    )


class TestTheIncident:
    def test_ten_day_silence_with_runs_continuing_is_an_alarm(self) -> None:
        health = _assess(
            last_run_requested_at=NOW - timedelta(minutes=1),
            last_decision_proposed_at=datetime(2026, 8, 7, 13, 33, tzinfo=timezone.utc),
            last_decision_executed_at=None,
        )

        assert health.verdict is TechLeadWriteVerdict.SILENT
        assert health.is_alarm
        assert "no tech-lead decision has EVER been applied" in health.reason

    def test_proposals_continuing_without_execution_names_the_approval_gate(
        self,
    ) -> None:
        # The measured cause: reset_retry/kill_hung_session default to
        # `propose`, so decisions become gated issues nobody approves.
        health = _assess(
            last_run_requested_at=NOW - timedelta(hours=1),
            last_decision_proposed_at=NOW - timedelta(hours=2),
            last_decision_executed_at=NOW - timedelta(days=30),
        )

        assert health.verdict is TechLeadWriteVerdict.PROPOSING_ONLY
        assert health.is_alarm
        assert "approval gate" in health.reason
        # The diagnosis must send the reader to the appliers, not the runs.
        assert "not the runs" in health.reason


class TestHealthyAndIdle:
    def test_a_recent_applied_decision_is_healthy(self) -> None:
        health = _assess(
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
            last_run_requested_at=NOW - timedelta(days=30),
            last_decision_proposed_at=NOW - timedelta(days=30),
            last_decision_executed_at=NOW - timedelta(days=30),
        )

        assert health.verdict is TechLeadWriteVerdict.IDLE
        assert not health.is_alarm

    def test_a_store_with_no_tech_lead_history_at_all_is_idle(self) -> None:
        assert _assess().verdict is TechLeadWriteVerdict.IDLE

    def test_an_old_run_with_a_fresh_decision_is_still_writing(self) -> None:
        # Execution recency wins: a decision landing proves the path works even
        # if no run has been requested lately.
        health = _assess(
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
            last_run_requested_at=NOW - timedelta(minutes=1),
            last_decision_executed_at=NOW - timedelta(hours=executed_hours_ago),
        )

        assert health.verdict is expected


class TestEvidenceIsCarried:
    def test_every_age_is_reported_alongside_the_verdict(self) -> None:
        health = _assess(
            last_run_requested_at=NOW - timedelta(hours=1),
            last_decision_proposed_at=NOW - timedelta(hours=2),
            last_decision_executed_at=NOW - timedelta(hours=100),
        )

        assert health.run_requested_age_hours == 1.0
        assert health.decision_proposed_age_hours == 2.0
        assert health.decision_executed_age_hours == 100.0
        assert health.stale_after_hours == WINDOW

    def test_a_nonpositive_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="stale_after_hours must be positive"):
            assess_tech_lead_write_health(
                TechLeadWriteActivity(), now=NOW, stale_after_hours=0
            )
