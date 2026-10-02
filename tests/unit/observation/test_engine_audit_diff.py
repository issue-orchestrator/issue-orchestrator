"""How anomalies move between two engine audits (#7490)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.contracts.engine_audit import (
    Anomaly,
    AnomalyKind,
    AuditSource,
    EngineAuditReport,
    NoProgressSection,
    RefusedWork,
    SourceReading,
    SourceStatus,
    StateChange,
)
from issue_orchestrator.observation.engine_audit_diff import (
    IncomparableAuditError,
    diff_reports,
    load_report,
)


def _anomaly(
    kind: AnomalyKind, subject: str, *sources: AuditSource, count: int | None = None
) -> Anomaly:
    return Anomaly(kind=kind, sources=sources, subject=subject, signature="sig", detail="d", count=count)


PARKED = _anomaly(AnomalyKind.PARKED_ACTION, "issue:410", AuditSource.ACTION_LIVENESS, count=3)
LABEL = _anomaly(AnomalyKind.ATTENTION_LABEL, "#411", AuditSource.GITHUB)
STALE = _anomaly(AnomalyKind.STALE_UNRESOLVED_WORK, "#7001", AuditSource.VALIDATED_WORK)
LOOP = _anomaly(AnomalyKind.NO_PROGRESS_LOG, "#410", AuditSource.LOG, AuditSource.TIMELINE, count=40)
LATER = "2026-09-29T10:00:00+00:00"
CHANGED_410 = (StateChange(subject="#410", at="2026-09-28T12:00:00+00:00"),)


def _report(
    *anomalies: Anomaly,
    at: str = "2026-09-28T10:00:00+00:00",
    repo: str = "o/r",
    github: SourceStatus = SourceStatus.READ,
    log: SourceStatus = SourceStatus.READ,
    timeline: SourceStatus = SourceStatus.READ,
    state_changes: tuple[StateChange, ...] = (),
    refused_work: tuple[RefusedWork, ...] = (),
) -> EngineAuditReport:
    status = {AuditSource.GITHUB: github, AuditSource.LOG: log, AuditSource.TIMELINE: timeline}
    return EngineAuditReport(
        generated_at=at,
        repo=repo,
        state_dir="/s",
        partial=any(v is not SourceStatus.READ for v in status.values()),
        sources=tuple(
            SourceReading(source=source, status=status.get(source, SourceStatus.READ))
            for source in AuditSource
        ),
        validated_work=None,
        action_liveness=None,
        tech_lead=None,
        claims=None,
        github=None,
        no_progress=NoProgressSection(
            window_start=at,
            window_end=at,
            log=None,
            log_signatures=(),
            refused_work=refused_work,
            timeline_repeats=(),
            state_changes=state_changes,
        ),
        fetch_cost=None,
        anomalies=anomalies,
    )


def test_new_resolved_and_persisting_are_keyed_by_what_the_anomaly_is() -> None:
    previous = _report(PARKED, LABEL, LOOP)
    grown = LOOP.model_copy(update={"count": 90, "detail": "worse"})
    current = _report(grown, STALE, at="2026-09-29T10:00:00+00:00")

    diff = diff_reports(previous, current)

    assert diff.previous_generated_at == previous.generated_at
    assert diff.new == (STALE,)
    assert set(diff.resolved) == {PARKED, LABEL}
    assert [(p.anomaly, p.previous_count) for p in diff.persisting] == [(grown, 40)]
    assert diff.unobserved == ()


def test_an_anomaly_whose_source_was_not_read_is_unobserved_not_resolved() -> None:
    """A rate-limited GitHub read must not turn every attention label into a fix."""
    previous = _report(PARKED, LABEL)
    current = _report(PARKED, github=SourceStatus.RATE_LIMITED)

    diff = diff_reports(previous, current)

    assert diff.resolved == ()
    assert diff.unobserved == (LABEL,)
    assert [p.anomaly for p in diff.persisting] == [PARKED]


@pytest.mark.parametrize(
    ("log", "timeline"),
    [
        # The tail began inside the window: the repeat may be in the part not read.
        (SourceStatus.INCOMPLETE, SourceStatus.READ),
        # "Since the last state change" needs the timeline too.
        (SourceStatus.READ, SourceStatus.ABSENT),
    ],
)
def test_a_log_anomaly_is_resolved_only_by_a_full_read_of_everything_it_rests_on(
    log, timeline
) -> None:
    diff = diff_reports(
        _report(LOOP), _report(log=log, timeline=timeline, at=LATER, state_changes=CHANGED_410)
    )

    assert (diff.resolved, diff.unobserved) == ((), (LOOP,))


def test_a_no_progress_anomaly_is_resolved_by_its_subject_changing_state() -> None:
    diff = diff_reports(_report(LOOP), _report(at=LATER, state_changes=CHANGED_410))

    assert (diff.resolved, diff.unobserved) == ((LOOP,), ())


@pytest.mark.parametrize(
    "state_changes",
    [
        (),  # the repeats aged out of the window (or were trimmed): nothing changed
        (StateChange(subject="#410", at="2026-09-28T09:00:00+00:00"),),  # before the previous audit
        (StateChange(subject="#411", at="2026-09-28T12:00:00+00:00"),),  # another subject
    ],
)
def test_a_no_progress_anomaly_that_only_aged_out_is_unobserved(state_changes) -> None:
    diff = diff_reports(_report(LOOP), _report(at=LATER, state_changes=state_changes))

    assert (diff.resolved, diff.unobserved) == ((), (LOOP,))


def test_audits_of_different_repositories_are_not_compared() -> None:
    with pytest.raises(IncomparableAuditError, match="o/r"):
        diff_reports(_report(repo="o/r"), _report(repo="o/other"))


def test_a_previous_report_round_trips_through_its_file(tmp_path: Path) -> None:
    path = tmp_path / "previous.json"
    report = _report(PARKED, LOOP)
    path.write_text(report.model_dump_json(), encoding="utf-8")

    assert load_report(path) == report


def test_a_previous_report_of_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "previous.json"
    payload = json.loads(_report(PARKED).model_dump_json())
    payload["schema_version"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IncomparableAuditError, match="schema 0"):
        load_report(path)


def test_a_malformed_previous_report_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "previous.json"
    payload = json.loads(_report(PARKED).model_dump_json())
    del payload["anomalies"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="anomalies"):
        load_report(path)


#: PR #379's review refused because #364 is blocked (porchpin, 2026-10-02).
REFUSED = Anomaly(kind=AnomalyKind.REFUSED_WORK, sources=(AuditSource.LOG, AuditSource.TIMELINE),
                  subject="PR #379", signature="review:issue_blocked", detail="d", count=9)
REFUSAL = RefusedWork(subject="PR #379", related=("#364",), action="review", reason="issue_blocked", loggers=("scanner",),
                      example="Skipping", count=9, since_state_change=9,
                      first_seen="2026-09-28T09:00:00+00:00", last_seen="2026-09-28T10:00:00+00:00")


@pytest.mark.parametrize(
    ("state_changes", "resolved"),
    [
        ((), False),  # aged out of the window: nothing moved
        ((StateChange(subject="PR #379", at="2026-09-28T12:00:00+00:00"),), True),
        # r2 F1: the issue whose block refused the review changed (unblocked).
        ((StateChange(subject="#364", at="2026-09-28T12:00:00+00:00"),), True),
        ((StateChange(subject="#364", at="2026-09-28T09:00:00+00:00"),), False),  # before the previous audit
        ((StateChange(subject="#365", at="2026-09-28T12:00:00+00:00"),), False),  # another subject
    ],
)
def test_refused_work_gone_is_resolved_only_by_a_change_of_a_subject_its_refusals_named(
    state_changes: tuple[StateChange, ...], resolved: bool
) -> None:
    diff = diff_reports(_report(REFUSED, refused_work=(REFUSAL,)), _report(at=LATER, state_changes=state_changes))

    assert (diff.resolved, diff.unobserved) == (((REFUSED,), ()) if resolved else ((), (REFUSED,)))
