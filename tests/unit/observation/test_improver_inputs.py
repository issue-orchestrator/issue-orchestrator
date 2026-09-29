"""Assembling the improver's evidence from an engine's records (#7490)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_inputs import ScorecardHead
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.pause_state import PauseActor, PauseReason, PauseTransition
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionResult,
    CharterProposalLifecycle,
    TechLeadCharterDecision,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.observation.improver_inputs import (
    Scorecard,
    UnparseableTimestampError,
    applied_at,
    case_files_input,
    charter_decisions_input,
    exam_series,
    interventions_input,
)
from issue_orchestrator.ports.engine_audit import (
    CaseFileObservation,
    CaseFileRecord,
    TechLeadRunHistoryRead,
    TimelineEvent,
)
from issue_orchestrator.ports.timeline_store import TimelineRecord

CUTOFF = datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc)
WINDOW_START = CUTOFF - timedelta(hours=24)


def _at(hours_before_cutoff: float) -> str:
    return (CUTOFF - timedelta(hours=hours_before_cutoff)).isoformat()


def _decision(action_id: str, kind: str, *, decided: str) -> TechLeadCharterDecision:
    return TechLeadCharterDecision.from_verdict(
        TechLeadCharterPolicy.from_config(Config()).decide(kind),
        decision_id=f"decision:run:{action_id}",
        source=CharterDecisionSource.DECISION,
        run_id="run",
        action_id=action_id,
        anchor_issue_number=410,
        target_number=410,
        target_is_pr=False,
        decided_at=decided,
        tracks_proposal=kind == "reset_retry",
    )


def _applied(decision: TechLeadCharterDecision, at: str | None) -> TechLeadCharterDecision:
    return replace(decision, execution=CharterExecutionResult.APPLIED, execution_at=at)


# -- applied_at -----------------------------------------------------------------


def test_applied_at_is_when_the_applier_committed_it() -> None:
    decision = _applied(_decision("A1", "post_comment", decided=_at(5)), _at(4))

    assert applied_at(decision) == CUTOFF - timedelta(hours=4)


def test_applied_at_of_an_approved_proposal_is_its_approval() -> None:
    decision = replace(
        _decision("A1", "reset_retry", decided=_at(5)),
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED,
        lifecycle_updated_at=_at(2),
    )

    assert applied_at(decision) == CUTOFF - timedelta(hours=2)


@pytest.mark.parametrize(
    "decision",
    [
        # Withheld: it never took effect.
        replace(
            _decision("A1", "post_comment", decided=_at(5)),
            execution=CharterExecutionResult.WITHHELD,
            execution_at=_at(4),
        ),
        # Applied, but with no time: decided_at never stands in for one.
        _applied(_decision("A1", "post_comment", decided=_at(5)), None),
        # Awaiting approval.
        _decision("A1", "reset_retry", decided=_at(5)),
    ],
)
def test_applied_at_is_absent_unless_an_application_time_is_recorded(decision) -> None:
    assert applied_at(decision) is None


def test_a_naive_stored_time_is_refused() -> None:
    decision = _applied(_decision("A1", "post_comment", decided=_at(5)), "2026-09-28T10:00:00")

    with pytest.raises(UnparseableTimestampError):
        applied_at(decision)


# -- charter-decisions.json -------------------------------------------------------


def test_the_window_keeps_decisions_made_in_it_and_older_ones_applied_in_it() -> None:
    old_applied_in_window = _applied(_decision("A1", "post_comment", decided=_at(40)), _at(3))
    old_applied_before = _applied(_decision("A2", "post_comment", decided=_at(40)), _at(30))
    in_window = _decision("A3", "post_comment", decided=_at(2))
    after_cutoff = _decision("A4", "post_comment", decided=(CUTOFF + timedelta(minutes=1)).isoformat())

    staged = charter_decisions_input(
        [old_applied_before, old_applied_in_window, in_window, after_cutoff],
        window_start=WINDOW_START,
        cutoff=CUTOFF,
    )

    assert [d.action_id for d in staged.decisions] == ["A1", "A3"]
    assert staged.decisions[0].applied_at == CUTOFF - timedelta(hours=3)


def test_a_ledger_older_than_the_window_covers_the_whole_window() -> None:
    staged = charter_decisions_input(
        [_decision("A1", "post_comment", decided=_at(40))], window_start=WINDOW_START, cutoff=CUTOFF
    )

    assert staged.coverage.complete
    assert staged.coverage.contains(WINDOW_START, CUTOFF)


def test_a_ledger_younger_than_the_window_covers_only_from_its_first_record() -> None:
    staged = charter_decisions_input(
        [_decision("A1", "post_comment", decided=_at(3))], window_start=WINDOW_START, cutoff=CUTOFF
    )

    assert staged.coverage.from_ == CUTOFF - timedelta(hours=3)
    assert not staged.coverage.contains(WINDOW_START, CUTOFF)


def test_an_empty_ledger_covers_nothing() -> None:
    staged = charter_decisions_input([], window_start=WINDOW_START, cutoff=CUTOFF)

    assert not staged.coverage.complete
    assert staged.coverage.from_ is None


# -- case-files.json ---------------------------------------------------------------


def _run(started: datetime, *, ended: datetime | None, detail: str = "diagnosed") -> TechLeadRunRecord:
    return TechLeadRunRecord(
        run_key="global:health_review",
        scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        phase=TechLeadRunPhase.COMPLETED if ended else TechLeadRunPhase.RUNNING,
        started_at=started,
        run_id=f"run-{started.isoformat()}",
        session_name="tech-lead-1",
        anchor_issue_number=418,
        ended_at=ended,
        detail=detail,
    )


def _case_file() -> CaseFileRecord:
    return CaseFileRecord(
        signature="sig", issue_number=372, recorded_at=_at(100), observation_count=2,
        fix_class="code", area="", diagnosis="the diagnosis", disposition="active",
        retirement_pending=False,
        observations=(CaseFileObservation("o1", _at(100)), CaseFileObservation("o2", _at(1))),
    )


def test_case_files_carry_their_bodies_and_runs_in_the_window_are_diagnoses() -> None:
    early = _run(CUTOFF - timedelta(hours=50), ended=CUTOFF - timedelta(hours=49))
    overlapping = _run(CUTOFF - timedelta(hours=25), ended=CUTOFF - timedelta(hours=23), detail="saw #410")
    running = _run(CUTOFF - timedelta(hours=1), ended=None)

    staged = case_files_input(
        [_case_file()],
        TechLeadRunHistoryRead(records=(early, overlapping, running), unreadable=0),
        window_start=WINDOW_START,
        cutoff=CUTOFF,
    )

    assert [c.body for c in staged.case_files] == ["the diagnosis"]
    assert [c.id for c in staged.case_files] == ["case-file:sig"]
    assert [d.body for d in staged.diagnoses] == ["saw #410", "diagnosed"]
    assert staged.coverage.contains(WINDOW_START, CUTOFF)


def test_the_run_history_never_proves_the_tech_lead_did_not_look() -> None:
    """Its writer drops a failed write, so its diagnoses are evidence of a
    look, never a complete record of every look."""
    staged = case_files_input(
        [_case_file()],
        TechLeadRunHistoryRead(records=(_run(CUTOFF - timedelta(hours=50), ended=CUTOFF),), unreadable=1),
        window_start=WINDOW_START,
        cutoff=CUTOFF,
    )

    assert not staged.diagnoses_coverage.complete
    assert "could not be read back" in staged.diagnoses_coverage.detail
    assert staged.coverage.complete  # the case-file ledger's own


def test_case_file_coverage_starts_at_the_ledgers_first_record() -> None:
    young = CaseFileRecord(
        signature="young", issue_number=1, recorded_at=_at(3), observation_count=1, fix_class="",
        area="", diagnosis="", disposition="active", retirement_pending=False,
        observations=(CaseFileObservation("y1", _at(3)),),
    )

    staged = case_files_input(
        [young], TechLeadRunHistoryRead(records=(), unreadable=0),
        window_start=WINDOW_START, cutoff=CUTOFF,
    )

    assert staged.coverage.from_ == CUTOFF - timedelta(hours=3)
    assert not case_files_input(
        [], TechLeadRunHistoryRead(records=(), unreadable=0), window_start=WINDOW_START, cutoff=CUTOFF
    ).coverage.complete


def test_what_was_recorded_after_the_cutoff_is_not_staged() -> None:
    """The snapshot is copied after the audit's cutoff; what lands between is
    outside the window the coverage speaks for."""
    late = CaseFileRecord(
        signature="late", issue_number=2, recorded_at=(CUTOFF + timedelta(minutes=1)).isoformat(),
        observation_count=1, fix_class="", area="", diagnosis="", disposition="active",
        retirement_pending=False, observations=(),
    )
    observed_late = replace(
        _case_file(),
        observations=(
            *_case_file().observations,
            CaseFileObservation("o3", (CUTOFF + timedelta(minutes=1)).isoformat()),
        ),
    )

    staged = case_files_input(
        [late, observed_late], TechLeadRunHistoryRead(records=(), unreadable=0),
        window_start=WINDOW_START, cutoff=CUTOFF,
    )

    assert [c.signature for c in staged.case_files] == ["sig"]
    assert [o.observation_id for o in staged.case_files[0].observations] == ["o1", "o2"]
    assert not charter_decisions_input(
        [_decision("A1", "post_comment", decided=(CUTOFF + timedelta(minutes=1)).isoformat())],
        window_start=WINDOW_START, cutoff=CUTOFF,
    ).coverage.complete


def test_a_run_time_in_a_daylight_saving_fold_widens_the_run_to_both_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """01:30 on the fall-back night happens twice; the record cannot say which."""
    import time

    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        ambiguous = datetime(2026, 11, 1, 1, 30)
        staged = case_files_input(
            [],
            TechLeadRunHistoryRead(records=(_run(ambiguous, ended=ambiguous),), unreadable=0),
            window_start=datetime(2026, 10, 31, tzinfo=timezone.utc),
            cutoff=datetime(2026, 11, 2, tzinfo=timezone.utc),
        )
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    [diagnosis] = staged.diagnoses
    assert diagnosis.started_at == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert diagnosis.ended_at == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)


def test_run_times_without_a_zone_are_the_engine_hosts_local_time() -> None:
    """The engine stamps sessions with ``datetime.now()``: naive local time."""
    local = datetime(2026, 9, 28, 10, 0)
    staged = case_files_input(
        [],
        TechLeadRunHistoryRead(records=(_run(local, ended=None),), unreadable=0),
        window_start=local.astimezone() - timedelta(hours=1),
        cutoff=local.astimezone() + timedelta(hours=1),
    )

    assert staged.diagnoses[0].started_at == local.astimezone()


# -- interventions.json ------------------------------------------------------------


def test_interventions_are_the_recorded_operator_acts_in_the_window() -> None:
    approved = replace(
        _decision("A1", "reset_retry", decided=_at(5)),
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED,
        lifecycle_updated_at=_at(4),
        proposal_issue_number=900,
    )
    declined_long_ago = replace(
        _decision("A2", "reset_retry", decided=_at(50)),
        lifecycle=CharterProposalLifecycle.DECLINED,
        lifecycle_updated_at=_at(49),
    )
    reset = TimelineEvent(
        issue_number=410,
        record=TimelineRecord(
            event_id="e1", timestamp=_at(3), event="issue.unblocked",
            data={"reason": "reset_retry_requested", "source": "dashboard"},
        ),
    )
    other = TimelineEvent(
        issue_number=411,
        record=TimelineRecord(event_id="e2", timestamp=_at(3), event="issue.unblocked", data={"reason": "x"}),
    )
    operator = PauseTransition(at=CUTOFF - timedelta(hours=2), paused=True, reason=PauseReason.OPERATOR, actor=PauseActor.CLI)
    breaker = PauseTransition(
        at=CUTOFF - timedelta(hours=2), paused=True, reason=PauseReason.LOOP_ERROR_THRESHOLD, actor=PauseActor.SYSTEM
    )

    staged = interventions_input(
        [approved, declined_long_ago], [reset, other], [operator, breaker],
        window_start=WINDOW_START, cutoff=CUTOFF,
    )

    assert [(i.kind, i.subject) for i in staged.interventions] == [
        ("proposal_approved", "#900"),
        ("reset_retry", "#410"),
        ("operator_pause", "engine"),
    ]
    assert staged.complete is False
    assert staged.not_derivable


# -- exam/ -------------------------------------------------------------------------


def _card(case_id: str, hours_ago: float) -> Scorecard:
    return Scorecard(
        written_at=CUTOFF - timedelta(hours=hours_ago),
        head=ScorecardHead(schema_version=1, case_id=case_id, engine_commit="abc", passed=True, failures=()),
        text="{}",
    )


def test_the_exam_series_is_the_latest_card_per_case_and_the_one_before() -> None:
    series = exam_series([_card("A", 30), _card("A", 5), _card("A", 60), _card("B", 5), _card("B", 30)])

    assert series.latest["A"].written_at == CUTOFF - timedelta(hours=5)
    assert series.previous["A"].written_at == CUTOFF - timedelta(hours=30)
    assert series.comparable


def test_exam_scores_over_different_case_sets_are_not_comparable() -> None:
    assert not exam_series([_card("A", 5), _card("A", 30), _card("B", 5)]).comparable
    assert not exam_series([]).comparable


def test_the_exam_writes_its_scorecards_somewhere_durable() -> None:
    """The improver stages the latest scorecards, so they must outlive the
    budgeted suite's temporary checkout (see ``test-tech-lead-exam``)."""
    makefile = (Path(__file__).resolve().parents[3] / "Makefile").read_text(encoding="utf-8")

    assert "E2E_EXAM_OUT=$(EXAM_OUT)" in makefile
    assert "EXAM_OUT ?= $(shell git rev-parse --path-format=absolute --git-common-dir)/io-tech-lead-exam" in makefile


def test_an_unread_timeline_is_named_among_what_is_not_derivable() -> None:
    staged = interventions_input([], "absent: no timeline.sqlite", [], window_start=WINDOW_START, cutoff=CUTOFF)

    assert not any("timeline" in source for source in staged.derived_from)
    assert any("absent: no timeline.sqlite" in gap for gap in staged.not_derivable)


def test_a_case_file_observed_after_the_cutoff_leaves_the_ledger_unproven() -> None:
    """The observation may have supplied its diagnosis after the cutoff (r3):
    the body as of the cutoff is unknown, so coverage is not complete."""
    observed_late = replace(
        _case_file(),
        observations=(CaseFileObservation("late", (CUTOFF + timedelta(minutes=1)).isoformat()),),
    )

    staged = case_files_input(
        [observed_late], TechLeadRunHistoryRead(records=(), unreadable=0),
        window_start=WINDOW_START, cutoff=CUTOFF,
    )

    assert not staged.coverage.complete
    assert "sig" in staged.coverage.detail


def test_a_result_linked_after_the_cutoff_is_not_part_of_what_the_audit_saw() -> None:
    """The snapshot is copied after the cutoff (r4 F2): an applier result or
    an approval linked in between is projected away."""
    late = (CUTOFF + timedelta(minutes=1)).isoformat()
    executed = _applied(_decision("A1", "post_comment", decided=_at(2)), late)
    approved = replace(
        _decision("A2", "reset_retry", decided=_at(2)),
        lifecycle=CharterProposalLifecycle.APPROVED_APPLIED,
        lifecycle_updated_at=late,
    )

    staged = charter_decisions_input([executed, approved], window_start=WINDOW_START, cutoff=CUTOFF)

    assert [(d.effect, d.applied_at) for d in staged.decisions] == [
        ("unlinked", None), ("awaiting_approval", None),
    ]
