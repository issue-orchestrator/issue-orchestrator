"""Staging the improver's inputs from a real engine state directory (#7490).

The stores are the production SQLite stores, written the way the engine
writes them; GitHub and the source export are fakes at their ports.
"""

from __future__ import annotations

import gc
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.engine_start import EngineStartRecord
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.domain.improver_findings_validation import validate_findings
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    TechLeadCharterDecision,
)
from issue_orchestrator.domain.tech_lead_run import TechLeadRunScopeKind
from issue_orchestrator.domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.entrypoints.improver_staging import (
    ImproverInputStager,
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    load_staged_evidence,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.engine_start_record import write_engine_start
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.infra.tech_lead_run_record_store import SqliteTechLeadRunRecordStore
from issue_orchestrator.ports.engine_audit import OpenIssueLabels
from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError
from issue_orchestrator.testing.exam.cases import EXAM_CASE_IDS

#: After the fixture's real-clock writes (the case-file ledger stamps its own
#: rows), so everything the fixture records is inside the audited window.
NOW = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=1)
STARTED = NOW - timedelta(hours=6)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class FakeHost:
    issues: list[OpenIssueLabels] = field(
        default_factory=lambda: [OpenIssueLabels(number=7491, title="Fetch cost", labels=("bug",))]
    )
    error: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def list_open_issue_labels_complete(self) -> list[OpenIssueLabels]:
        self.calls.append("issues")
        if self.error is not None:
            raise self.error
        return self.issues

    def list_open_prs_complete(self) -> list:
        self.calls.append("prs")
        return []


@dataclass
class FakeSource:
    exported: list[str] = field(default_factory=list)
    error: Exception | None = None

    def export(self, commit: str, destination: Path) -> None:
        if self.error is not None:
            raise self.error
        self.exported.append(commit)
        (destination / "src").mkdir(parents=True)
        (destination / "src" / "engine.py").write_text("# engine\n")


def _decision(action_id: str, decided: datetime) -> TechLeadCharterDecision:
    return TechLeadCharterDecision.from_verdict(
        TechLeadCharterPolicy.from_config(Config()).decide("post_comment"),
        decision_id=f"decision:run:{action_id}",
        source=CharterDecisionSource.DECISION,
        run_id="run",
        action_id=action_id,
        anchor_issue_number=410,
        target_number=410,
        target_is_pr=False,
        decided_at=decided.isoformat(),
        tracks_proposal=False,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state = tmp_path / "engine" / ".issue-orchestrator" / "state"
    state.mkdir(parents=True)
    authority = SqliteTechLeadAuthorityStore(state / "tech_lead_authority.sqlite")
    authority.charter_ledger.record_decisions(
        [_decision("A1", NOW - timedelta(hours=40)), _decision("A2", NOW - timedelta(hours=2))]
    )
    authority.record_pattern(
        signature="retry-refused", issue_number=372, observation_id="o1", diagnosis="refused every tick"
    )
    SqliteTechLeadRunRecordStore(state / "tech_lead_runs.sqlite").open_run(
        TechLeadRunRecord(
            run_key="global:health_review",
            scope_kind=TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW,
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
            phase=TechLeadRunPhase.RUNNING,
            started_at=(NOW - timedelta(hours=30)).replace(tzinfo=None),
            run_id="run",
            session_name="tech-lead-1",
            anchor_issue_number=418,
        )
    )
    write_engine_start(
        state,
        EngineStartRecord(
            started_at=STARTED, engine_commit=COMMIT, package_version="0.10.0",
            repo_root=str(state.parent.parent), repo_head=None,
            charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        ),
    )
    del authority
    gc.collect()
    return state


def _request(state: Path, tmp_path: Path, **overrides: object) -> ImproverStagingRequest:
    values: dict[str, object] = {
        "state_dir": state,
        "audited_repo": "porchpin/porchpin",
        "outputs_repo": "issue-orchestrator/issue-orchestrator",
        "run_dir": tmp_path / "run",
        "previous_audit": None,
        "exam_dir": None,
        "window": timedelta(hours=24),
        "log_tail_bytes": 1024 * 1024,
    }
    values.update(overrides)
    return ImproverStagingRequest(**values)  # type: ignore[arg-type]


def _stager(audited: FakeHost, outputs: FakeHost, source: FakeSource | None = None) -> ImproverInputStager:
    return ImproverInputStager(
        audited_host=audited, outputs_host=outputs, source=source or FakeSource(), clock=lambda: NOW
    )


def _fingerprint(state: Path) -> dict[str, tuple[int, int]]:
    return {
        p.relative_to(state).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in state.rglob("*")
        if p.is_file()
    }


def test_stages_every_input_the_prompt_lists_from_the_engine_records(state: Path, tmp_path: Path) -> None:
    audited, outputs, source = FakeHost(), FakeHost(), FakeSource()
    before = _fingerprint(state)

    staged = _stager(audited, outputs, source).stage(_request(state, tmp_path))

    data = staged.data_dir
    assert {p.name for p in data.iterdir()} == {
        "audit.json", "engine-start.json", "charter.json", "charter-decisions.json",
        "case-files.json", "interventions.json", "open-issues.json", "inputs.json", "exam",
        "engine-source",
    }
    decisions = json.loads((data / "charter-decisions.json").read_text())
    assert [d["action_id"] for d in decisions["decisions"]] == ["A2"]
    # A live ledger's commit boundary is not proven yet (#7525).
    assert decisions["coverage"]["complete"] is False and "#7525" in decisions["coverage"]["detail"]
    assert datetime.fromisoformat(decisions["coverage"]["from"]) == NOW - timedelta(hours=24)
    cases = json.loads((data / "case-files.json").read_text())
    assert [c["body"] for c in cases["case_files"]] == ["refused every tick"]
    assert len(cases["diagnoses"]) == 1
    start = json.loads((data / "engine-start.json").read_text())
    assert start["engine_commit"] == COMMIT
    assert json.loads((data / "charter.json").read_text())["roles"]["general"]["authority"] == "propose"
    assert source.exported == [COMMIT]
    assert audited.calls == ["issues", "prs"] and outputs.calls == ["issues"]
    manifest = json.loads((data / "inputs.json").read_text())
    missing = {i["name"] for i in manifest["inputs"] if not i["staged"]}
    assert missing == {"audit-previous.json", "audit-diff.json", "exam"}
    assert set(manifest["existing_exam_case_ids"]) == set(EXAM_CASE_IDS)
    # The audited engine's state is only read: nothing in it changed.
    assert _fingerprint(state) == before


def test_one_repository_is_listed_once_for_the_audit_and_open_issues(state: Path, tmp_path: Path) -> None:
    host = FakeHost()

    staged = _stager(host, FakeHost(error=AssertionError("unused"))).stage(
        _request(state, tmp_path, outputs_repo="porchpin/porchpin")
    )

    assert host.calls.count("issues") == 1
    assert json.loads((staged.data_dir / "open-issues.json").read_text())["issues"][0]["title"] == "Fetch cost"


def test_a_previous_audit_is_staged_with_the_diff(state: Path, tmp_path: Path) -> None:
    first = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path / "one"))

    second = _stager(FakeHost(), FakeHost()).stage(
        _request(state, tmp_path / "two", previous_audit=first.data_dir / "audit.json")
    )

    assert (second.data_dir / "audit-previous.json").is_file()
    assert (second.data_dir / "audit-diff.json").is_file()
    assert second.audit.diff is not None


def test_an_engine_that_never_recorded_its_start_is_unavailable(state: Path, tmp_path: Path) -> None:
    (state / "engine-start.json").unlink()

    with pytest.raises(ImproverInputsUnavailable, match="engine-start.json"):
        _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))


def test_open_issues_that_cannot_be_read_make_the_run_unavailable(state: Path, tmp_path: Path) -> None:
    limited = RepositoryHostRateLimitedError("API rate limit exceeded")
    limited.rate_limit = HostRateLimit(resets_at=NOW + timedelta(minutes=5), kind="primary")

    with pytest.raises(ImproverInputsUnavailable, match="open issues"):
        _stager(FakeHost(), FakeHost(error=limited)).stage(_request(state, tmp_path))


def test_an_engine_source_that_cannot_be_exported_makes_the_run_unavailable(state: Path, tmp_path: Path) -> None:
    with pytest.raises(ImproverInputsUnavailable, match="engine source"):
        _stager(FakeHost(), FakeHost(), FakeSource(error=RuntimeError("no such commit"))).stage(
            _request(state, tmp_path)
        )


def test_without_a_tech_lead_store_its_inputs_are_named_missing(state: Path, tmp_path: Path) -> None:
    for name in ("tech_lead_authority.sqlite", "tech_lead_authority.sqlite-wal", "tech_lead_authority.sqlite-shm"):
        (state / name).unlink(missing_ok=True)

    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))

    missing = {i.name for i in staged.manifest.inputs if not i.staged}
    assert {"charter-decisions.json", "case-files.json", "interventions.json"} <= missing


def test_the_latest_two_scorecards_of_each_case_are_staged(state: Path, tmp_path: Path) -> None:
    exam = tmp_path / "exam-out"
    exam.mkdir()

    def card(case_id: str, stamp: str, mtime: datetime, passed: bool) -> None:
        path = exam / f"{case_id}-{COMMIT[:10]}-{stamp}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "case_id": case_id, "engine_commit": COMMIT,
            "passed": passed, "failures": [] if passed else ["goal x"], "title": "t",
        }))
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
        (exam / f"{case_id}-{COMMIT[:10]}-{stamp}.observation.json").write_text("{}")

    card("A-one", "1", NOW - timedelta(days=2), False)
    card("A-one", "2", NOW - timedelta(days=1), True)
    card("Z-new", "2", NOW - timedelta(days=1), True)

    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path, exam_dir=exam))

    names = sorted(p.name for p in (staged.data_dir / "exam").iterdir())
    assert names == ["A-one.json", "A-one.previous.json", "Z-new.json"]
    assert json.loads((staged.data_dir / "exam" / "A-one.json").read_text())["passed"] is True
    assert staged.manifest.exam_scores_comparable is False
    assert "Z-new" in staged.manifest.existing_exam_case_ids


def test_what_was_staged_reads_back_as_the_validators_evidence(state: Path, tmp_path: Path) -> None:
    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))

    evidence = load_staged_evidence(staged.data_dir)

    assert evidence.engine_start.engine_commit == COMMIT
    assert evidence.engine_source_files == {"src/engine.py"}
    assert evidence.decisions is not None and evidence.charter is not None
    empty = {
        "schema_version": 1, "engine_commit": COMMIT, "engine_started_at": STARTED.isoformat(),
        "findings": [],
        "trend": {"exam_scores": "unobserved", "operator_interventions": "unobserved", "notes": ""},
    }
    assert validate_findings(json.dumps(empty), evidence).findings == ()


def test_a_tech_lead_store_without_its_case_file_tables_is_unreadable_not_empty(
    state: Path, tmp_path: Path
) -> None:
    """Opening the store on a copy would CREATE the missing tables, so a
    damaged ledger would read as an engine that never filed a case."""
    with sqlite3.connect(state / "tech_lead_authority.sqlite") as conn:
        conn.execute("DROP TABLE tech_lead_pattern_observations")

    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))

    missing = {i.name: i.detail for i in staged.manifest.inputs if not i.staged}
    assert "case-files.json" in missing
    assert "tech_lead_pattern_observations" in missing["case-files.json"]


def test_an_audit_told_not_to_read_github_still_stages_the_open_issues(state: Path, tmp_path: Path) -> None:
    from issue_orchestrator.contracts.engine_audit import SourceStatus
    from issue_orchestrator.observation.engine_audit import Unavailable

    outputs = FakeHost()
    stager = ImproverInputStager(
        audited_host=Unavailable(SourceStatus.SKIPPED, "--no-github"),
        outputs_host=outputs,
        source=FakeSource(),
        clock=lambda: NOW,
    )

    staged = stager.stage(_request(state, tmp_path, outputs_repo="porchpin/porchpin"))

    assert outputs.calls == ["issues"]
    assert json.loads((staged.data_dir / "open-issues.json").read_text())["issues"][0]["number"] == 7491


def test_the_case_file_ledger_is_staged_without_a_run_history(state: Path, tmp_path: Path) -> None:
    for name in ("tech_lead_runs.sqlite", "tech_lead_runs.sqlite-wal", "tech_lead_runs.sqlite-shm"):
        (state / name).unlink(missing_ok=True)

    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))

    cases = json.loads((staged.data_dir / "case-files.json").read_text())
    assert [c["signature"] for c in cases["case_files"]] == ["retry-refused"]
    assert cases["coverage"]["complete"] is False and "#7525" in cases["coverage"]["detail"]
    assert cases["diagnoses"] == [] and cases["diagnoses_coverage"]["complete"] is False
    assert "tech-lead run history absent" in cases["diagnoses_coverage"]["detail"]


def test_no_decision_dated_inside_coverage_is_missing_from_the_copy(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """3a r6: the engine keeps writing while staging copies its stores. A
    decision recorded after the ledger was copied must fall outside the
    coverage staging claims, so the cutoff is taken before any copy."""
    from issue_orchestrator.entrypoints import improver_staging

    real = improver_staging.snapshot_tech_lead_runs

    def engine_writes_after_the_ledger_copy(state_dir, *args, **kwargs):  # type: ignore[no-untyped-def]
        store = SqliteTechLeadAuthorityStore(state_dir / "tech_lead_authority.sqlite")
        store.charter_ledger.record_decisions([_decision("LATE", datetime.now(UTC))])
        del store
        gc.collect()
        return real(state_dir, *args, **kwargs)

    monkeypatch.setattr(improver_staging, "snapshot_tech_lead_runs", engine_writes_after_the_ledger_copy)
    stager = ImproverInputStager(
        audited_host=FakeHost(), outputs_host=FakeHost(), source=FakeSource(),
        clock=lambda: datetime.now(UTC),
    )

    staged = stager.stage(_request(state, tmp_path))

    decisions = json.loads((staged.data_dir / "charter-decisions.json").read_text())
    live = SqliteTechLeadAuthorityStore(state / "tech_lead_authority.sqlite").charter_ledger.list_all()
    [late] = [d for d in live if d.action_id == "LATE"]
    assert datetime.fromisoformat(late.decided_at) > datetime.fromisoformat(decisions["coverage"]["to"])


def test_an_unreadable_scorecard_is_left_out_and_named(state: Path, tmp_path: Path) -> None:
    """One written in place mid-run, or cut short by a crash (3b r1 F6): the
    run goes on without it, and no exam trend is comparable."""
    exam = tmp_path / "exam-out"
    exam.mkdir()
    for stamp in ("1", "2"):
        (exam / f"A-one-x-{stamp}.json").write_text(json.dumps({
            "schema_version": 1, "case_id": "A-one", "engine_commit": COMMIT,
            "passed": True, "failures": [],
        }))
    (exam / "A-one-x-3.json").write_text('{"schema_version": 1, "case_')

    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path, exam_dir=exam))

    entry = next(i for i in staged.manifest.inputs if i.name == "exam")
    assert "A-one-x-3.json" in entry.detail
    assert staged.manifest.exam_scores_comparable is False
