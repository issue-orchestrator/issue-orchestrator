"""Staging the improver's inputs from a real engine state directory (#7490).

The stores are the production SQLite stores, written the way the engine
writes them; GitHub and the source export are fakes at their ports.
"""

from __future__ import annotations

import gc
import json
import logging
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
from issue_orchestrator.adapters.registered_engine_inventory import engine_at
from issue_orchestrator.entrypoints.improver_staging import (
    ImproverInputStager,
    ImproverInputsUnavailable,
    ImproverStagingRequest,
    load_staged_evidence,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.engine_start_record import write_engine_start
from issue_orchestrator.infra.logging_config import ROTATING_LOG_DATEFMT, ROTATING_LOG_FORMAT
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.infra.tech_lead_run_record_store import SqliteTechLeadRunRecordStore
from issue_orchestrator.ports.engine_audit import OpenIssueLabels
from issue_orchestrator.ports.pull_request_tracker import PRInfo
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
    prs: list[PRInfo] = field(default_factory=list)

    def list_open_issue_labels_complete(self) -> list[OpenIssueLabels]:
        self.calls.append("issues")
        if self.error is not None:
            raise self.error
        return self.issues

    def list_open_prs_complete(self) -> list[PRInfo]:
        self.calls.append("prs")
        return self.prs


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
    return make_engine_state(tmp_path / "engine")


def make_engine_state(root: Path, repo: str = "porchpin/porchpin") -> Path:
    """A small engine's state under ``root``: decisions, a case file, a
    tech-lead run and its start record."""
    state = root / ".issue-orchestrator" / "state"
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
            started_at=STARTED, repo=repo, engine_commit=COMMIT, package_version="0.10.0",
            repo_root=str(state.parent.parent), repo_head=None,
            charter=TechLeadCharterPolicy.from_config(Config()).effective_charter(),
        ),
    )
    del authority
    gc.collect()
    return state


def _request(state: Path, tmp_path: Path, **overrides: object) -> ImproverStagingRequest:
    values: dict[str, object] = {
        "engine": engine_at(state, "porchpin/porchpin"),
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
        "engine-source", "blocked-items.json",
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


def test_state_an_engine_wrote_for_another_repository_is_never_staged_as_this_one(
    state: Path, tmp_path: Path
) -> None:
    """r3 F1: the config was re-pointed since the engine started; its state
    stays attributed to the repository it started for."""
    with pytest.raises(ImproverInputsUnavailable, match="started for porchpin/porchpin"):
        _stager(FakeHost(), FakeHost()).stage(
            _request(state, tmp_path, engine=engine_at(state, "someone/else"))
        )


def test_what_was_staged_reads_back_as_the_validators_evidence(state: Path, tmp_path: Path) -> None:
    staged = _stager(FakeHost(), FakeHost()).stage(_request(state, tmp_path))

    evidence = load_staged_evidence(staged.data_dir)

    assert evidence.engine_start.engine_commit == COMMIT
    assert evidence.engine_source_files == {"src/engine.py"}
    assert evidence.decisions is not None and evidence.charter is not None
    assert (evidence.engine_id, evidence.audited_repo) == (
        engine_at(state, "porchpin/porchpin").engine_id, "porchpin/porchpin"
    )
    empty = {
        "schema_version": 5, "engine_commit": COMMIT, "engine_started_at": STARTED.isoformat(), "design_findings": [],
        "findings": [], "blocked_items": [],
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


# -- blocked-items.json: the operator's objective (handover grade #1) ---------


def _timeline(state: Path, issue: int, name: str, at: datetime, data: dict) -> None:
    from issue_orchestrator.execution.timeline_store import SqliteTimelineStore
    from issue_orchestrator.ports.timeline_store import TimelineRecord

    SqliteTimelineStore(state / "timeline.sqlite").append(
        issue,
        TimelineRecord(
            event_id=f"{name}-{issue}-{at.isoformat()}", timestamp=at.isoformat(), event=name,
            data={"issue_number": issue, **data}, source_event=name,
        ),
    )


def _blocked_engine(state: Path) -> FakeHost:
    """The handover-#1 shapes: #262 an agent's question, #326 an unexplained
    block re-added after a removal, #364 a parked publish a case file names."""
    from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore

    claims = SqlitePendingWorkClaimStore(state / "pending_work_claims.sqlite")
    claims.record_needs_human_cause(262, "agent_completion", reason="agent requested needs_human on completion")
    claims.record_needs_human_cause(
        364, "action_liveness", reason="parked recover_validated_work: pr_labels may not contain needs-human"
    )
    claims.record_needs_human_cause(999, "session_lifecycle", reason="a closed issue's stale row")
    question = "Should I split #262: land this branch under 'Refs #262'?"
    _timeline(state, 262, "issue.needs_human", NOW - timedelta(hours=50),
              {"question": question, "reason": "Agent requested human input"})
    _timeline(state, 326, "issue.labels_changed", NOW - timedelta(hours=60),
              {"added": ["needs-human"], "removed": []})
    _timeline(state, 326, "issue.labels_changed", NOW - timedelta(hours=59),
              {"added": [], "removed": ["needs-human"]})
    _timeline(state, 326, "issue.labels_changed", NOW - timedelta(hours=10),
              {"added": ["needs-human"], "removed": []})
    _timeline(state, 364, "issue.labels_changed", NOW - timedelta(hours=5),
              {"added": ["needs-human"], "removed": []})
    _timeline(state, 364, "issue.labels_changed", NOW - timedelta(hours=4),
              {"added": ["pr-pending"], "removed": []})
    authority = SqliteTechLeadAuthorityStore(state / "tech_lead_authority.sqlite")
    old = TechLeadCharterDecision.from_verdict(
        TechLeadCharterPolicy.from_config(Config()).decide("post_comment"),
        decision_id="decision:old:262", source=CharterDecisionSource.DECISION, run_id="old",
        action_id="C1", anchor_issue_number=262, target_number=262, target_is_pr=False,
        decided_at=(NOW - timedelta(hours=45)).isoformat(), tracks_proposal=False,
    )
    authority.charter_ledger.record_decisions([old])
    authority.record_pattern(
        signature="exchange-escalation-pr-label-refused", issue_number=436, observation_id="o2",
        diagnosis="On #364 / PR #379 the coder used --pr-labels needs-human; refused every round.",
    )
    del authority, claims
    gc.collect()
    return FakeHost(issues=[
        OpenIssueLabels(number=7491, title="Fetch cost", labels=("bug",)),
        OpenIssueLabels(number=262, title="Seller index", labels=("needs-human", "priority:high")),
        OpenIssueLabels(number=326, title="Deletion", labels=("needs-human", "blocked-cross-milestone")),
        OpenIssueLabels(number=364, title="Provenance", labels=("needs-human", "recovery-pending")),
    ])


def test_every_blocked_item_is_staged_with_its_cause_onset_and_tech_lead_record(
    state: Path, tmp_path: Path
) -> None:
    audited = _blocked_engine(state)
    before = _fingerprint(state)

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    blocked = json.loads((staged.data_dir / "blocked-items.json").read_text())
    items = {i["number"]: i for i in blocked["items"]}
    assert sorted(items) == [262, 326, 364]
    # The cause rows, byte for byte, and only the item's own.
    assert items[262]["needs_human_causes"] == [
        {"cause": "agent_completion", "reason": "agent requested needs_human on completion"}
    ]
    assert items[326]["needs_human_causes"] == []
    # The agent's question is in the item's own events. A request is no onset
    # (it is also emitted for a label already on): with no recorded add, unknown.
    assert any("Should I split #262" in e["detail"] for e in items[262]["block_events"])
    assert items[262]["blocked_since"] is None
    # A removal forgets an earlier add: #326 has been blocked since the re-add.
    [needs_human_326] = [b for b in items[326]["blocking_labels"] if b["label"] == "needs-human"]
    assert datetime.fromisoformat(needs_human_326["since_at"]) == NOW - timedelta(hours=10)
    # blocked-cross-milestone was never seen put on: the item's onset is unknown.
    assert items[326]["blocked_since"] is None
    # A decision from BEFORE the observation window is still the item's record.
    assert [d["decision_id"] for d in items[262]["decisions"]] == ["decision:old:262"]
    assert "decision:old:262" not in {
        d["decision_id"] for d in json.loads((staged.data_dir / "charter-decisions.json").read_text())["decisions"]
    }
    assert items[364]["case_file_ids"] == ["case-file:exchange-escalation-pr-label-refused"]
    # Every blocked item is an attention anomaly, whatever its blocking label.
    attention = {(a["subject"], a["signature"]) for a in staged.audit.model_dump()["anomalies"]
                 if a["kind"] == "attention_label"}
    assert ("#326", "blocked-cross-milestone") in attention
    # One listing served the audit and the blocked items.
    assert audited.calls.count("issues") == 1
    assert _fingerprint(state) == before
    evidence = load_staged_evidence(staged.data_dir)
    assert evidence.blocked_items is not None and len(evidence.blocked_items.items) == 3


def test_a_blocked_items_pr_whose_review_is_dropped_every_scan_is_its_stalled_work(
    state: Path, tmp_path: Path
) -> None:
    """porchpin #364 / PR #379 (2026-10-02): published work queued for review
    and dropped on every scan while the issue is blocked, logged at INFO. The
    item names its PR, the PR's pipeline, and the refused work."""
    audited = _blocked_engine(state)
    audited.prs = [PRInfo(number=379, title="Provenance", url="u", branch="364-provenance", body="Closes #364",
                          state="open", labels=["needs-code-review"], draft=False)]
    queued = NOW - timedelta(minutes=40)
    _timeline(state, 364, "pr.view_changed", queued, {"pr_number": 379, "added": ["needs-code-review"], "removed": []})
    _timeline(state, 364, "review.queued", queued + timedelta(seconds=1), {"pr_number": 379})
    _timeline(state, 364, "review.skipped", queued + timedelta(seconds=30),
              {"pr_number": 379, "reason": "stale_pending_review:issue_blocked"})
    formatter = logging.Formatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT)

    def line(at: datetime) -> str:
        record = logging.LogRecord(
            "issue_orchestrator.control.pr_scanner", logging.INFO, __file__, 1,
            "[SCANNER] Skipping stale review PR: pr=379 issue=364 reason=issue_blocked", None, None,
        )
        record.created, record.msecs = at.timestamp(), 0
        return formatter.format(record)

    log = state / "logs" / "orchestrator.log"
    log.parent.mkdir()
    log.write_text("".join(line(queued + timedelta(minutes=2 + 3 * m)) + "\n" for m in range(8)), encoding="utf-8")

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    item = {i["number"]: i for i in json.loads((staged.data_dir / "blocked-items.json").read_text())["items"]}[364]
    assert [(p["number"], p["draft"]) for p in item["open_prs"]] == [(379, False)]
    assert [e["event"] for e in item["open_prs"][0]["pipeline_events"]] == [
        "pr.view_changed", "review.queued", "review.skipped",
    ]
    assert [(w["kind"], w["subject"], w["signature"]) for w in item["stalled_work"]] == [
        ("refused_work", "PR #379", "review:issue_blocked"),
    ]
    assert ("refused_work", "PR #379", "review:issue_blocked") in {a.key for a in staged.audit.anomalies}
    # The audit's PR listing served the blocked items: one GitHub walk.
    assert audited.calls.count("prs") == 1


def test_blocked_items_are_missing_when_the_audited_issues_were_not_read(state: Path, tmp_path: Path) -> None:
    from issue_orchestrator.contracts.engine_audit import SourceStatus
    from issue_orchestrator.observation.engine_audit import Unavailable

    stager = ImproverInputStager(
        audited_host=Unavailable(SourceStatus.SKIPPED, "--no-github"),
        outputs_host=FakeHost(), source=FakeSource(), clock=lambda: NOW,
    )

    staged = stager.stage(_request(state, tmp_path))

    entry = next(i for i in staged.manifest.inputs if i.name == "blocked-items.json")
    assert not entry.staged and "skipped" in entry.detail
    assert not (staged.data_dir / "blocked-items.json").exists()


def test_a_blind_run_hides_the_excluded_issues_from_open_issues(state: Path, tmp_path: Path) -> None:
    outputs = FakeHost(issues=[
        OpenIssueLabels(number=7491, title="Fetch cost", labels=("bug",)),
        OpenIssueLabels(number=7592, title="Recovery pr_labels", labels=()),
        OpenIssueLabels(number=7593, title="Triage blocked items", labels=()),
    ])

    staged = _stager(FakeHost(), outputs).stage(
        _request(state, tmp_path, excluded_open_issues=frozenset({7592, 7593}))
    )

    issues = json.loads((staged.data_dir / "open-issues.json").read_text())["issues"]
    assert [i["number"] for i in issues] == [7491]
    entry = next(i for i in staged.manifest.inputs if i.name == "open-issues.json")
    # Not even the numbers: naming them would point the improver at them.
    assert "7592" not in entry.detail and "7593" not in entry.detail


def test_blocked_items_unread_after_the_issue_listing_make_the_run_unavailable(state: Path, tmp_path: Path) -> None:
    """r2 F1: the issues read, then the PR listing hit a rate limit. The audit
    drops its GitHub section, so no blocked item could be accounted for: the
    run must not accept findings that silently drop every live block."""
    limited = RepositoryHostRateLimitedError("API rate limit exceeded")
    limited.rate_limit = HostRateLimit(resets_at=NOW + timedelta(minutes=5), kind="primary")

    class PrsLimited(FakeHost):
        def list_open_prs_complete(self) -> list:
            raise limited

    audited = PrsLimited(issues=[OpenIssueLabels(number=364, title="parked", labels=("needs-human",))])

    with pytest.raises(ImproverInputsUnavailable, match="blocked-items.json"):
        _stager(audited, FakeHost()).stage(_request(state, tmp_path))


def test_a_prefixed_engines_blocked_items_are_read_by_its_recorded_label_policy(tmp_path: Path) -> None:
    """#7490 r3 F1: an engine with label_prefix "bot" blocks with
    bot:needs-human; read with the default names it would have none."""
    from issue_orchestrator.contracts.engine_start import LabelPolicy
    from issue_orchestrator.infra.engine_start_record import read_engine_start

    state = make_engine_state(tmp_path / "engine")
    record = read_engine_start(state)
    write_engine_start(state, record.model_copy(update={
        "labels": LabelPolicy(
            prefix="bot", needs_human="needs-human", blocked="blocked", provider_unavailable="provider-unavailable"
        ),
    }))
    audited = FakeHost(issues=[
        OpenIssueLabels(number=364, title="parked", labels=("bot:needs-human",)),
        OpenIssueLabels(number=365, title="unprefixed", labels=("needs-human-ish",)),
    ])

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    blocked = json.loads((staged.data_dir / "blocked-items.json").read_text())
    assert [i["number"] for i in blocked["items"]] == [364]
    assert "prefix bot" in blocked["blocking_rule"]
    attention = {(a.subject, a.signature) for a in staged.audit.anomalies if a.kind.value == "attention_label"}
    assert ("#364", "bot:needs-human") in attention


def test_an_engine_that_recorded_no_label_policy_is_read_on_the_defaults_and_says_so(
    state: Path, tmp_path: Path
) -> None:
    from issue_orchestrator.infra.engine_start_record import read_engine_start

    write_engine_start(state, read_engine_start(state).model_copy(update={"labels": None}))
    audited = FakeHost(issues=[OpenIssueLabels(number=364, title="parked", labels=("needs-human",))])

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    blocked = json.loads((staged.data_dir / "blocked-items.json").read_text())
    assert [i["number"] for i in blocked["items"]] == [364]
    assert "recorded no label policy" in blocked["blocking_rule"]


def test_a_renamed_provider_outage_label_is_a_blocked_item(tmp_path: Path) -> None:
    """#7490 r4 F1: the circuit breaker's label is configurable and blocking."""
    from issue_orchestrator.contracts.engine_start import LabelPolicy
    from issue_orchestrator.infra.engine_start_record import read_engine_start

    state = make_engine_state(tmp_path / "engine")
    write_engine_start(state, read_engine_start(state).model_copy(update={
        "labels": LabelPolicy(
            prefix=None, needs_human="needs-human", blocked="blocked", provider_unavailable="waiting-for-provider"
        ),
    }))
    audited = FakeHost(issues=[OpenIssueLabels(number=400, title="waits", labels=("waiting-for-provider",))])

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    blocked = json.loads((staged.data_dir / "blocked-items.json").read_text())
    assert [i["number"] for i in blocked["items"]] == [400]
    assert ("#400", "waiting-for-provider") in {
        (a.subject, a.signature) for a in staged.audit.anomalies if a.kind.value == "attention_label"
    }


def test_a_case_variant_tech_lead_marker_is_a_blocked_item(state: Path, tmp_path: Path) -> None:
    """#7490 r5 F1: GitHub label names are case-insensitive."""
    audited = FakeHost(issues=[OpenIssueLabels(number=179, title="human work", labels=("Tech-Lead-Needs-Human",))])

    staged = _stager(audited, FakeHost()).stage(_request(state, tmp_path))

    blocked = json.loads((staged.data_dir / "blocked-items.json").read_text())
    assert [i["number"] for i in blocked["items"]] == [179]
    assert ("#179", "Tech-Lead-Needs-Human") in {
        (a.subject, a.signature) for a in staged.audit.anomalies if a.kind.value == "attention_label"
    }
