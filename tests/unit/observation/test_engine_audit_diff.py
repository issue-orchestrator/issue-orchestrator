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
    SourceReading,
    SourceStatus,
)
from issue_orchestrator.observation.engine_audit_diff import (
    IncomparableAuditError,
    diff_reports,
    load_report,
)


def _anomaly(kind: AnomalyKind, subject: str, source: AuditSource, count: int | None = None) -> Anomaly:
    return Anomaly(kind=kind, source=source, subject=subject, signature="sig", detail="d", count=count)


PARKED = _anomaly(AnomalyKind.PARKED_ACTION, "issue:410", AuditSource.ACTION_LIVENESS, 3)
LABEL = _anomaly(AnomalyKind.ATTENTION_LABEL, "#411", AuditSource.GITHUB)
STALE = _anomaly(AnomalyKind.STALE_UNRESOLVED_WORK, "#7001", AuditSource.VALIDATED_WORK)
LOOP = _anomaly(AnomalyKind.NO_PROGRESS_LOG, "the engine", AuditSource.LOG, 40)


def _report(
    *anomalies: Anomaly,
    at: str = "2026-09-28T10:00:00+00:00",
    repo: str = "o/r",
    github: SourceStatus = SourceStatus.READ,
) -> EngineAuditReport:
    return EngineAuditReport(
        generated_at=at,
        repo=repo,
        state_dir="/s",
        partial=github is not SourceStatus.READ,
        sources=tuple(
            SourceReading(source=source, status=github if source is AuditSource.GITHUB else SourceStatus.READ)
            for source in AuditSource
        ),
        validated_work=None,
        action_liveness=None,
        tech_lead=None,
        claims=None,
        github=None,
        no_progress=NoProgressSection(
            window_start=at, window_end=at, log=None, log_signatures=(), timeline_repeats=()
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
