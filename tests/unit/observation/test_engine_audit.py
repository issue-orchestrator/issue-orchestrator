"""``io engine-audit`` end to end over an engine state directory (#7490).

Every database is created and filled through its owning store, in that
store's real schema, then audited through the CLI's own composition: the
snapshots, the stores opened on them, the log and the timeline. GitHub is
the one port faked at its boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from issue_orchestrator.adapters.github import GitHubAdapter
from issue_orchestrator.contracts.engine_audit import (
    AnomalyKind,
    AuditSource,
    EngineAuditReport,
    SourceStatus,
)
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import PendingRework
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from issue_orchestrator.domain.tech_lead_findings import PromotedFinding
from issue_orchestrator.entrypoints.cli_tools import engine_audit as cli
from issue_orchestrator.execution.action_liveness_store import SQLiteActionLivenessStore
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.execution.timeline_store import (
    SqliteTimelineAuditReader,
    SqliteTimelineStore,
)
from issue_orchestrator.infra.logging_config import ROTATING_LOG_DATEFMT, ROTATING_LOG_FORMAT
from issue_orchestrator.infra.sqlite_snapshot import snapshot_sqlite
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.infra.validated_work_census import SqliteValidatedWorkCensus
from issue_orchestrator.observation import engine_audit as observed
from issue_orchestrator.observation.engine_audit import (
    EngineAuditInputs,
    Unavailable,
    audit_engine,
)
from issue_orchestrator.ports.pending_work_claim_store import QuarantineCause
from issue_orchestrator.ports.timeline_store import TimelineRecord
from tests.unit.test_github_http import _client_with_transport
from tests.unit.test_tech_lead_charter_ledger import _decision
from tests.unit.validated_work_support import Rig, begin, capture, claim, finalize

NOW = datetime.now(UTC).replace(microsecond=0)
REPO = "owner/repo"


# -- the engine's state, written by its own stores ---------------------------


def _validated_work(state: Path) -> None:
    rig = Rig(state / cli.VALIDATED_WORK_DB)
    store = rig.open()
    published = capture(issue=6914)
    store.admit(published)
    token = claim(store, published)
    finalize(store, token, begin(store, token))
    # Admitted two days ago and still queued: stuck.
    store.admit(capture(issue=7001, branch="stuck", at=(NOW - timedelta(days=2)).isoformat()))
    # Admitted an hour ago: still in flight, not stuck.
    store.admit(capture(issue=7002, branch="fresh", at=(NOW - timedelta(hours=1)).isoformat()))


def _liveness(state: Path) -> None:
    store = SQLiteActionLivenessStore(state / cli.ACTION_LIVENESS_DB)
    failed = NOW - timedelta(hours=3)
    store.put(
        LivenessRow(
            key=LivenessKey(ActionIdentity("issue:410", "settle_promotion"), "fp-1", 410),
            attempts=3,
            first_failed_at=failed,
            last_failed_at=failed,
            last_outcome=OutcomeKind.PERMANENT,
            last_reason="pattern is terminal",
            next_attempt_at=None,
            escalated=True,
        )
    )
    store.put(
        LivenessRow(
            key=LivenessKey(ActionIdentity("engine", "settle_promotion"), "fp-2"),
            attempts=1,
            first_failed_at=failed,
            last_failed_at=failed,
            last_outcome=OutcomeKind.TRANSIENT,
            last_reason="transport",
            next_attempt_at=NOW + timedelta(minutes=5),
        )
    )
    store.request_pause(411, "Has forbidden labels")


def _tech_lead(state: Path) -> None:
    store = SqliteTechLeadAuthorityStore(state / cli.TECH_LEAD_AUTHORITY_DB)
    store.charter_ledger.record_decisions(
        [
            _decision("A1", "kill_hung_session", at="2026-09-26T10:00:00+00:00"),
            _decision("A2", "create_issue", target=None, at="2026-09-26T11:00:00+00:00"),
            _decision("A3", "create_issue", target=None, at="2026-09-26T12:00:00+00:00"),
        ]
    )
    for n, state_name in enumerate(("promoted", "promoted", "declined")):
        store.record_promotion(
            promotion=PromotedFinding(
                signature=f"sig-{n}",
                case_file_issue_number=100 + n,
                target_repo="io/io",
                target_issue_number=200 + n,
                state=state_name,
            )
        )


def _claims(state: Path, make_session) -> None:
    store = SqlitePendingWorkClaimStore(state / cli.PENDING_WORK_CLAIMS_DB)
    for issue, deferred in ((5, False), (6, True)):
        session = make_session(issue_number=issue)
        store.hold_pending_work_claim(
            session.run_assets,
            PendingWorkClaim(
                PendingWorkKind.REWORK,
                PendingRework(GitHubIssueKey(REPO, str(issue)), "agent:coder", pr_number=90 + issue),
            ),
            issue_number=issue,
        )
        if deferred:
            store.defer_pending_work_claim(session.run_assets)
    store.record_quarantine(
        "q-7",
        run_key="run-7",
        session_name="coding-7",
        issue_number=7,
        error="terminal still alive",
        cause=QuarantineCause.RUN_UNRESTORABLE,
        work_kind=None,
    )


def _timeline(state: Path) -> None:
    store = SqliteTimelineStore(state / cli.TIMELINE_DB)
    reason = {"reason": "Has forbidden labels: frozenset({'io:needs-reconcile'})"}
    at = NOW - timedelta(hours=2)
    for n in range(6):
        store.append(410, _event("reconciliation.required", at + timedelta(minutes=n), reason))
    # #411 repeats the same failure, but its labels change halfway.
    for n in range(6):
        if n == 3:
            store.append(411, _event("issue.labels_changed", at + timedelta(minutes=n, seconds=30), {}))
        store.append(411, _event("reconciliation.required", at + timedelta(minutes=n), reason))
    # Outside the window: not read.
    store.append(410, _event("issue.labels_changed", NOW - timedelta(days=3), {}))
    store.close()


def _event(name: str, at: datetime, data: dict) -> TimelineRecord:
    return TimelineRecord(
        event_id=f"{name}-{at.isoformat()}", timestamp=at.isoformat(), event=name,
        data=data, source_event=name,
    )


def _log(state: Path) -> None:
    formatter = logging.Formatter(ROTATING_LOG_FORMAT, datefmt=ROTATING_LOG_DATEFMT)

    def line(message: str, at: datetime, level: int = logging.WARNING, logger: str = "io") -> str:
        record = logging.LogRecord(logger, level, __file__, 1, message, None, None)
        record.created = at.timestamp()
        record.msecs = 0
        return formatter.format(record)

    at = NOW - timedelta(hours=1)
    lines = [
        line("Failed to settle promoted finding 'sig-0'", at + timedelta(minutes=m), logging.ERROR)
        for m in range(7)
    ]
    lines += [line("Reconciliation failed for issue #411: labels", at + timedelta(minutes=m)) for m in range(6)]
    for cycle in range(3):
        lines.append(line(f"[LOOP] Iteration {cycle} - active=1", at + timedelta(minutes=cycle), logging.INFO))
        lines += [
            line(f'HTTP Request: GET https://api.github.com/repos/o/r/issues/{n} "HTTP/1.1 200 OK"',
                 at + timedelta(minutes=cycle), logging.INFO, "httpx")
            for n in (1, 2, 1)
        ]
    lines += [
        line(f"[FETCH-COST] mode={mode} trigger=scheduled gh_calls={calls} gh_errors=0"
             f" duration_ms=1000 refreshed_issues=5", at + timedelta(minutes=m), logging.INFO)
        for m, (mode, calls) in enumerate((("full", 57), ("incremental", 90), ("incremental", 100)))
    ]
    log = state / cli.ENGINE_LOG
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def state(tmp_path: Path, make_session) -> Path:
    state = tmp_path / "engine" / ".issue-orchestrator" / "state"
    state.mkdir(parents=True)
    _validated_work(state)
    _liveness(state)
    _tech_lead(state)
    _claims(state, make_session)
    _timeline(state)
    _log(state)
    return state


# -- GitHub, faked at its port ------------------------------------------------


@dataclass
class _Issue:
    number: int
    labels: tuple[str, ...]


@dataclass
class _PR:
    number: int
    draft: bool | None


@dataclass
class FakeHost:
    issues: list[_Issue] = field(
        default_factory=lambda: [
            _Issue(410, ("needs-human", "tech-lead-needs-human")),
            _Issue(411, ("io:needs-reconcile", "in-progress")),
            _Issue(412, ()),
        ]
    )
    prs: list[_PR] = field(default_factory=lambda: [_PR(90, True), _PR(91, False), _PR(92, False)])
    calls: list[str] = field(default_factory=list)

    def list_issues(self, labels=None, milestone=None, state="open", limit=100,
                    required_stable_ids=None, *, exhaustive=False):
        self.calls.append(f"issues state={state} limit={limit} exhaustive={exhaustive}")
        return self.issues

    def list_open_prs_complete(self):
        self.calls.append("prs")
        return self.prs


def _run(state: Path, tmp_path: Path, monkeypatch, host, *extra: str) -> EngineAuditReport:
    monkeypatch.setattr(cli, "create_repository_host", lambda repo: host)
    out = tmp_path / f"report-{len(list(tmp_path.glob('report-*')))}.json"
    assert cli.main(["--state-dir", str(state), "--repo", REPO, "--output", str(out), *extra]) == 0
    return EngineAuditReport.model_validate_json(out.read_text(encoding="utf-8"))


def _kinds(report: EngineAuditReport) -> dict[AnomalyKind, set[tuple[str, str]]]:
    found: dict[AnomalyKind, set[tuple[str, str]]] = {}
    for anomaly in report.anomalies:
        found.setdefault(anomaly.kind, set()).add((anomaly.subject, anomaly.signature))
    return found


# -- the audit -----------------------------------------------------------------


def test_the_audit_reports_every_store_through_its_owner(state, tmp_path, monkeypatch) -> None:
    host = FakeHost()

    report = _run(state, tmp_path, monkeypatch, host)

    assert report.partial is False
    vw = report.validated_work
    assert vw is not None
    assert {(c.key, c.count) for c in vw.by_state_resolution} == {
        (("recovered", "published"), 1),
        (("queued", ""), 2),
    }
    assert [(w.issue_number, w.age_hours >= 47) for w in vw.unresolved] == [(7001, True), (7002, False)]

    lv = report.action_liveness
    assert lv is not None
    assert [(a.action, a.rows, a.parked, a.escalated, a.max_attempts) for a in lv.by_action] == [
        ("settle_promotion", 2, 1, 1, 3)
    ]
    assert [(p.subject, p.escalated) for p in lv.parked] == [("issue:410", True)]
    assert [p.issue_number for p in lv.owed_pauses] == [411]

    tl = report.tech_lead
    assert tl is not None
    assert [(c.key, c.count) for c in tl.charter_by_role_outcome] == [
        (("flow", "executed"), 2),
        (("flow", "proposed"), 1),
    ]
    assert [d.action_kind for d in tl.recent_decisions] == ["create_issue", "create_issue", "kill_hung_session"]
    assert {(c.key, c.count) for c in tl.promotions_by_state} == {(("promoted",), 2), (("declined",), 1)}

    claims = report.claims
    assert claims is not None
    assert (claims.held, claims.deferred) == (1, 1)
    assert [(q.issue_number, q.cause) for q in claims.quarantined] == [(7, "run_unrestorable")]

    gh = report.github
    assert gh is not None
    assert (gh.open_issues, gh.open_prs_ready, gh.draft_prs) == (3, 2, (90,))
    assert {(c.key[0], c.count) for c in gh.label_counts} >= {("needs-human", 1), ("in-progress", 1)}
    # Two listings, both complete-or-raise: no per-label or per-issue reads.
    assert host.calls == ["issues state=open limit=2000 exhaustive=True", "prs"]


def test_anomalies_name_what_is_stuck(state, tmp_path, monkeypatch) -> None:
    kinds = _kinds(_run(state, tmp_path, monkeypatch, FakeHost()))

    stale = kinds[AnomalyKind.STALE_UNRESOLVED_WORK]
    assert {subject for subject, _ in stale} == {"#7001"}
    assert kinds[AnomalyKind.PARKED_ACTION] == {("issue:410", "settle_promotion:fp-1")}
    assert kinds[AnomalyKind.OWED_PAUSE] == {("#411", "reconcile_pause")}
    assert kinds[AnomalyKind.QUARANTINED_CLAIM] == {("#7", "q-7")}
    assert kinds[AnomalyKind.ATTENTION_LABEL] == {
        ("#410", "needs-human"),
        ("#410", "tech-lead-needs-human"),
        ("#411", "io:needs-reconcile"),
    }
    assert kinds[AnomalyKind.DRAFT_PR] == {("PR #90", "draft")}
    # #410 repeats with no state change; #411's labels changed after its third.
    assert {s for s, _ in kinds[AnomalyKind.NO_PROGRESS_TIMELINE]} == {"#410"}
    log = kinds[AnomalyKind.NO_PROGRESS_LOG]
    assert ("the engine", "ERROR io: Failed to settle promoted finding 'sig-N'") in log
    assert ("#411", "WARNING io: Reconciliation failed for issue #N: labels") in log
    assert kinds[AnomalyKind.FETCH_COST_INVERTED] == {("the engine", "incremental_over_full")}
    assert kinds[AnomalyKind.REPEATED_ISSUE_READS] == {("the engine", "repeat_issue_reads")}


def test_log_repeats_after_a_state_change_count_from_it(state, tmp_path, monkeypatch) -> None:
    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (reconcile,) = [s for s in report.no_progress.log_signatures if s.subject == "#411"]
    assert (reconcile.count, reconcile.since_state_change) == (6, 6)

    # Timeline says #411's labels changed after its failures: none since.
    changed = NOW - timedelta(minutes=1)
    SqliteTimelineStore(state / cli.TIMELINE_DB).append(
        411, _event("issue.labels_changed", changed, {})
    )
    report = _run(state, tmp_path, monkeypatch, FakeHost())
    (reconcile,) = [s for s in report.no_progress.log_signatures if s.subject == "#411"]
    assert (reconcile.count, reconcile.since_state_change) == (6, 0)


def test_the_live_state_is_never_written(state, tmp_path, monkeypatch) -> None:
    """Every database, log and write-ahead log is byte-identical afterwards.

    ``-shm`` is excluded: it is SQLite's shared lock index, which every reader
    of a WAL database (``mode=ro`` included) marks, the engine's own too.
    """

    def digest() -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(state.rglob("*"))
            if p.is_file() and not p.name.endswith("-shm")
        }

    before = digest()

    _run(state, tmp_path, monkeypatch, FakeHost())

    assert digest() == before


def test_a_database_the_engine_never_created_is_absent_not_a_crash(state, tmp_path, monkeypatch) -> None:
    (state / cli.ACTION_LIVENESS_DB).unlink()
    for sidecar in state.glob(cli.ACTION_LIVENESS_DB + "-*"):
        sidecar.unlink()

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert report.partial is True
    assert report.action_liveness is None
    assert [(r.source, r.status) for r in report.sources if r.status is not SourceStatus.READ] == [
        (AuditSource.ACTION_LIVENESS, SourceStatus.ABSENT)
    ]


def _rate_limited_adapter() -> GitHubAdapter:
    resets = int((NOW + timedelta(minutes=30)).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(resets)},
            json={"message": "API rate limit exceeded"},
        )

    return GitHubAdapter(repo=REPO, http_client=_client_with_transport(httpx.MockTransport(handler)))


def test_a_rate_limited_github_read_is_a_partial_report(state, tmp_path, monkeypatch) -> None:
    report = _run(state, tmp_path, monkeypatch, _rate_limited_adapter())

    assert report.partial is True
    assert report.github is None
    (reading,) = [r for r in report.sources if r.source is AuditSource.GITHUB]
    assert reading.status is SourceStatus.RATE_LIMITED
    assert reading.resets_at == (NOW + timedelta(minutes=30)).isoformat()
    # Everything else was still read.
    assert report.validated_work is not None and report.tech_lead is not None


def test_a_github_failure_that_is_not_a_rate_limit_is_not_hidden(state, tmp_path, monkeypatch) -> None:
    class Broken(FakeHost):
        def list_open_prs_complete(self):
            raise RuntimeError("malformed listing")

    with pytest.raises(RuntimeError, match="malformed listing"):
        _run(state, tmp_path, monkeypatch, Broken())


def test_an_open_pr_without_a_draft_flag_is_refused(state, tmp_path, monkeypatch) -> None:
    host = FakeHost(prs=[_PR(1, None)])

    with pytest.raises(ValueError, match="draft"):
        _run(state, tmp_path, monkeypatch, host)


# -- reading the snapshot, never the live file ---------------------------------


def test_a_snapshot_sees_committed_pages_still_in_the_live_log(tmp_path: Path) -> None:
    live = tmp_path / "live.sqlite"
    store = SQLiteActionLivenessStore(live)  # held open: its commits stay in the -wal
    store.request_pause(9, "drift")
    assert (tmp_path / "live.sqlite-wal").exists()

    copy = snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    assert [p.issue_number for p in SQLiteActionLivenessStore(copy).pending_pauses()] == [9]


def test_a_closed_wal_database_is_copied_without_creating_its_sidecars(tmp_path: Path) -> None:
    """The validated-work store's usual state: WAL mode, no -wal or -shm."""
    live = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(live)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    assert not (tmp_path / "live.sqlite-wal").exists()

    copy = snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    with closing(sqlite3.connect(copy)) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]
    assert sorted(p.name for p in tmp_path.glob("live.sqlite*")) == ["live.sqlite"]


def test_a_writer_closing_between_the_log_check_and_the_open_is_read_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """The -wal seen by the check is gone by the open: read the closed file instead."""
    live = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(live)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (7)")
        conn.commit()
    log = tmp_path / "live.sqlite-wal"
    log.write_bytes(b"")

    def writer_closes(path, **_kwargs):
        log.unlink()  # the last writer closed: SQLite removes its log
        raise ReadOnlySqliteAccessError(ReadOnlySqliteFailure.UNREADABLE, "unable to open")

    monkeypatch.setattr("issue_orchestrator.infra.sqlite_snapshot.open_sqlite_readonly", writer_closes)

    copy = snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    with closing(sqlite3.connect(copy)) as conn:
        assert conn.execute("SELECT x FROM t").fetchall() == [(7,)]


def test_a_database_that_changes_under_every_copy_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(live)) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    stamps = iter(range(100))
    monkeypatch.setattr(
        "issue_orchestrator.infra.sqlite_snapshot._identity", lambda _path: next(stamps)
    )

    with pytest.raises(ReadOnlySqliteAccessError) as refused:
        snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    assert refused.value.reason is ReadOnlySqliteFailure.UNREADABLE
    assert not (tmp_path / "copy.sqlite").exists()


def test_the_timeline_reader_refuses_a_schema_it_does_not_know(tmp_path: Path) -> None:
    """Opening the store would DROP a mismatched table; the reader must not read "nothing"."""
    db = tmp_path / "timeline.sqlite"
    SqliteTimelineStore(db).append(1, _event("issue.labels_changed", NOW, {}))
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()

    with pytest.raises(ReadOnlySqliteAccessError) as refused:
        list(SqliteTimelineAuditReader(db, timeout=10.0).events_since(NOW - timedelta(days=1)))

    assert refused.value.reason is ReadOnlySqliteFailure.UNSUPPORTED_SCHEMA


def test_the_census_counts_unresolved_by_state_not_by_terminal_at(tmp_path: Path) -> None:
    """``terminal_at`` is '' (never NULL) on an unresolved row: the prototype's
    ``terminal_at IS NULL`` found none. The census reads the domain's states."""
    state = tmp_path / "state"
    state.mkdir()
    _validated_work(state)

    census = SqliteValidatedWorkCensus(state / cli.VALIDATED_WORK_DB, timeout=10.0).census()

    assert [(r.issue_number, r.state) for r in census.unresolved] == [(7001, "queued"), (7002, "queued")]
    assert census.unresolved[0].created_at == NOW - timedelta(days=2)


def test_the_auditor_refuses_a_naive_instant() -> None:
    absent = Unavailable(SourceStatus.ABSENT, "none")
    inputs = EngineAuditInputs(
        repo=REPO, state_dir=Path("/nowhere"), validated_work=absent, action_liveness=absent,
        tech_lead=absent, claims=absent, timeline=absent, log=absent, github=absent,
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        audit_engine(inputs, now=datetime(2026, 9, 28), window=timedelta(hours=1))
    report = audit_engine(inputs, now=NOW, window=timedelta(hours=1))
    assert report.partial is True and report.anomalies == ()


def test_the_attention_labels_are_the_prototype_set_without_in_progress() -> None:
    assert "in-progress" not in observed.ATTENTION_LABELS
    assert set(observed.ATTENTION_LABELS) == {
        "needs-human", "tech-lead-needs-human", "blocked-failed", "recovery-pending",
        "io:needs-reconcile", "proposed-tech-lead",
    }


def test_the_report_json_is_the_contract(state, tmp_path, monkeypatch) -> None:
    report = _run(state, tmp_path, monkeypatch, FakeHost())
    payload = json.loads(report.model_dump_json())

    assert payload["schema_version"] == 1
    payload["surprise"] = True
    with pytest.raises(ValueError):
        EngineAuditReport.model_validate(payload)


def test_previous_report_diff_through_the_cli(state, tmp_path, monkeypatch) -> None:
    first = _run(state, tmp_path, monkeypatch, FakeHost())
    previous = tmp_path / "previous.json"
    previous.write_text(first.model_dump_json(), encoding="utf-8")
    # The owed pause is settled; a new draft PR appears.
    SQLiteActionLivenessStore(state / cli.ACTION_LIVENESS_DB).clear_pause(411)
    host = FakeHost()
    host.prs.append(_PR(93, True))

    second = _run(state, tmp_path, monkeypatch, host, "--previous", str(previous))

    diff = second.diff
    assert diff is not None
    assert [(a.kind, a.subject) for a in diff.new] == [(AnomalyKind.DRAFT_PR, "PR #93")]
    assert [(a.kind, a.subject) for a in diff.resolved] == [(AnomalyKind.OWED_PAUSE, "#411")]
    assert len(diff.persisting) == len(first.anomalies) - 1


def test_the_cli_command_forwards_its_arguments(monkeypatch) -> None:
    from issue_orchestrator.entrypoints import cli as io_cli

    seen: list = []
    monkeypatch.setattr(cli, "run", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(
        "sys.argv",
        ["issue-orchestrator", "engine-audit", "--state-dir", "s", "--repo", "o/r", "--no-github"],
    )

    assert io_cli.main() == 0
    (args,) = seen
    assert (args.state_dir, args.repo, args.no_github, args.window_hours, args.log_tail_mb) == (
        "s", "o/r", True, 24.0, 64,
    )
