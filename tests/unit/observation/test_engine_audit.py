"""``io engine-audit`` end to end over an engine state directory (#7490).

Every database is created and filled through its owning store, in that
store's real schema, then audited through the CLI's own composition: the
snapshots, the stores opened on them, the log and the timeline. GitHub is
the one port faked at its boundary.
"""

from __future__ import annotations

import gc
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
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
from issue_orchestrator.entrypoints import engine_snapshot
from issue_orchestrator.entrypoints.cli_tools import engine_audit as cli
from issue_orchestrator.execution.action_liveness_store import SQLiteActionLivenessStore
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.execution.timeline_store import (
    SqliteTimelineAuditReader,
    SqliteTimelineStore,
)
from issue_orchestrator.infra import engine_log_reader
from issue_orchestrator.infra.logging_config import ROTATING_LOG_DATEFMT, ROTATING_LOG_FORMAT
from issue_orchestrator.infra.sqlite_snapshot import snapshot_sqlite
from issue_orchestrator.infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore
from issue_orchestrator.infra.validated_work_census import SqliteValidatedWorkCensus
from issue_orchestrator.observation import engine_audit as observed
from issue_orchestrator.domain.session_kind import SessionKind
from issue_orchestrator.observation.engine_audit import (
    STALE_UNRESOLVED_AFTER,
    BlockedLane,
    EngineAuditInputs,
    Unavailable,
    audit_engine,
)
from issue_orchestrator.ports.pending_work_claim_store import QuarantineCause
from issue_orchestrator.ports.timeline_store import TimelineRecord
from tests.unit.test_github_http import _client_with_transport
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterExecutionLink,
    CharterExecutionResult,
    decision_key,
)
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
            _decision("A4", "create_issue", target=None, at="2026-09-26T12:30:00+00:00"),
        ]
    )
    # What the applier then did, linked back through the ledger's own owner.
    store.charter_ledger.link_execution_outcomes(
        [
            CharterExecutionLink(decision_key("run-1", "A2"), CharterExecutionResult.WITHHELD, "held by gh guard"),
            CharterExecutionLink(decision_key("run-1", "A3"), CharterExecutionResult.WITHHELD, "held by gh guard"),
            CharterExecutionLink(decision_key("run-1", "A4"), CharterExecutionResult.APPLIED),
        ],
        at="2026-09-26T13:00:00+00:00",
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
    # Written before the audit window: proves the log read reaches back to it.
    lines = [line("engine started", NOW - timedelta(hours=30), logging.INFO)]
    lines += [
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
    # The fixture's writers are done: close their connections now (the last
    # close checkpoints the WAL into the main file), so no writer's checkpoint
    # can land in the middle of a test that compares the live bytes.
    gc.collect()
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

    def list_open_issue_labels_complete(self):
        self.calls.append("issues")
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
        (("flow", "executed"), 3),
        (("flow", "proposed"), 1),
    ]
    # An executed action is not an applied one: the applier withheld two.
    assert [(c.key, c.count) for c in tl.charter_effects] == [
        (("flow", "create_issue", "executed", "withheld"), 2),
        (("flow", "create_issue", "executed", "applied"), 1),
        (("flow", "kill_hung_session", "proposed", "awaiting_approval"), 1),
    ]
    assert [(d.effect, d.took_effect, d.execution_reason) for d in tl.recent_decisions] == [
        ("applied", True, None),
        ("withheld", False, "held by gh guard"),
        ("withheld", False, "held by gh guard"),
        ("awaiting_approval", False, None),
    ]
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
    assert host.calls == ["issues", "prs"]


def test_anomalies_name_what_is_stuck(state, tmp_path, monkeypatch) -> None:
    report = _run(state, tmp_path, monkeypatch, FakeHost())
    kinds = _kinds(report)
    # A repeat "since the subject last changed state" rests on the timeline too.
    assert {a.sources for a in report.anomalies if a.kind is AnomalyKind.NO_PROGRESS_LOG} == {
        (AuditSource.LOG, AuditSource.TIMELINE)
    }

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


def test_a_timeline_repeat_a_later_state_change_ended_is_not_current(
    state, tmp_path, monkeypatch
) -> None:
    """#410 failed six times; its labels then change: it progressed."""
    SqliteTimelineStore(state / cli.TIMELINE_DB).append(
        410, _event("issue.labels_changed", NOW - timedelta(minutes=10), {})
    )

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert report.no_progress.timeline_repeats == ()
    assert AnomalyKind.NO_PROGRESS_TIMELINE not in _kinds(report)


def test_nothing_written_after_the_audit_instant_counts(state, tmp_path) -> None:
    """The engine keeps writing while it is audited; the report's window has ended."""
    later = NOW + timedelta(minutes=5)
    timeline = SqliteTimelineStore(state / cli.TIMELINE_DB)
    for n in range(6):
        timeline.append(412, _event("reconciliation.required", later + timedelta(seconds=n), {"reason": "x"}))
    timeline.close()
    stamp = later.astimezone().strftime(ROTATING_LOG_DATEFMT)
    log = state / cli.ENGINE_LOG
    log.write_text(
        log.read_text(encoding="utf-8")
        + "".join(f"{stamp} [WARNING] io: Late failure for issue #412\n" for _ in range(6)),
        encoding="utf-8",
    )
    with tempfile.TemporaryDirectory() as scratch:
        args = cli.build_parser().parse_args(["--state-dir", str(state), "--repo", REPO, "--no-github"])
        report = audit_engine(
            cli._inputs(state, Path(scratch), args), now=NOW, window=timedelta(hours=24)
        )

    assert all(r.subject != "#412" for r in report.no_progress.timeline_repeats)
    assert all(s.subject != "#412" for s in report.no_progress.log_signatures)


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
    """Every file in the state directory is byte-identical afterwards, lock
    index included, and no file is added, whether or not a writer holds a
    database open."""

    def digest() -> dict[str, str]:
        return {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(state.rglob("*"))
            if p.is_file()
        }

    # No writer holds the state open (see the fixture), so any change is the audit's.
    assert not list(state.glob("*.sqlite-wal"))
    before = digest()

    _run(state, tmp_path, monkeypatch, FakeHost())

    assert digest() == before

    # And with the engine's writer holding one open, its -wal and -shm present.
    held = SQLiteActionLivenessStore(state / cli.ACTION_LIVENESS_DB)
    held.request_pause(412, "drift")
    assert (state / (cli.ACTION_LIVENESS_DB + "-shm")).exists()
    before = digest()

    _run(state, tmp_path, monkeypatch, FakeHost())

    assert digest() == before


def test_the_report_is_never_written_into_the_engine_state(state, tmp_path, monkeypatch) -> None:
    db = state / cli.VALIDATED_WORK_DB
    before = db.read_bytes()
    link = tmp_path / "innocent.json"
    link.symlink_to(db)
    monkeypatch.setattr(cli, "create_repository_host", lambda repo: FakeHost())

    for target in (db, state / cli.ENGINE_LOG, link):
        with pytest.raises(SystemExit, match="refusing to write"):
            cli.main(["--state-dir", str(state), "--repo", REPO, "--output", str(target)])

    assert db.read_bytes() == before


def test_a_report_path_hard_linked_to_engine_state_does_not_write_through(
    state, tmp_path, monkeypatch
) -> None:
    db = state / cli.VALIDATED_WORK_DB
    before = db.read_bytes()
    link = tmp_path / "report.json"
    link.hardlink_to(db)
    monkeypatch.setattr(cli, "create_repository_host", lambda repo: FakeHost())

    assert cli.main(["--state-dir", str(state), "--repo", REPO, "--output", str(link)]) == 0

    assert db.read_bytes() == before
    assert EngineAuditReport.model_validate_json(link.read_text(encoding="utf-8")).repo == REPO


def test_a_pr_state_change_ends_the_repeats_logged_against_that_pr(
    state, tmp_path, monkeypatch
) -> None:
    log = state / cli.ENGINE_LOG
    stamp = (NOW - timedelta(minutes=30)).astimezone().strftime(ROTATING_LOG_DATEFMT)
    log.write_text(
        log.read_text(encoding="utf-8")
        + "".join(f"{stamp} [WARNING] io: Merge queue refused PR #42 ({n})\n" for n in range(5)),
        encoding="utf-8",
    )
    SqliteTimelineStore(state / cli.TIMELINE_DB).append(
        7, _event("pr.view_changed", NOW - timedelta(minutes=5), {"issue_number": 7, "pr_number": 42})
    )

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (refused,) = [s for s in report.no_progress.log_signatures if s.subject == "PR #42"]
    assert (refused.count, refused.since_state_change) == (5, 0)
    assert all(subject != "PR #42" for subject, _ in _kinds(report).get(AnomalyKind.NO_PROGRESS_LOG, ()))


def test_a_qualified_reference_to_the_audited_repo_is_that_issue(state, tmp_path, monkeypatch) -> None:
    log = state / cli.ENGINE_LOG
    stamp = (NOW - timedelta(minutes=30)).astimezone().strftime(ROTATING_LOG_DATEFMT)
    log.write_text(
        log.read_text(encoding="utf-8")
        + "".join(f"{stamp} [WARNING] io: Could not read promoted issue {REPO}#411\n" for _ in range(5)),
        encoding="utf-8",
    )

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    subjects = {s.subject for s in report.no_progress.log_signatures if "promoted issue" in s.signature}
    assert subjects == {"#411"}


def test_without_the_timeline_no_repeat_is_claimed_since_a_state_change(
    state, tmp_path, monkeypatch
) -> None:
    (state / cli.TIMELINE_DB).unlink()
    for sidecar in state.glob(cli.TIMELINE_DB + "-*"):
        sidecar.unlink()

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (reconcile,) = [s for s in report.no_progress.log_signatures if s.subject == "#411"]
    assert (reconcile.count, reconcile.since_state_change) == (6, None)
    assert AnomalyKind.NO_PROGRESS_LOG not in _kinds(report)


def test_a_log_read_that_starts_inside_the_window_is_incomplete(state, tmp_path, monkeypatch) -> None:
    log = state / cli.ENGINE_LOG
    log.write_text("\n".join(log.read_text(encoding="utf-8").splitlines()[1:]) + "\n", encoding="utf-8")

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert report.partial is True
    (reading,) = [r for r in report.sources if r.source is AuditSource.LOG]
    assert reading.status is SourceStatus.INCOMPLETE
    assert report.no_progress.log is not None and report.no_progress.log.covers_window is False
    # What was read is still reported.
    assert report.no_progress.log_signatures


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


def test_a_snapshot_of_an_open_database_touches_none_of_its_files(tmp_path: Path) -> None:
    """Sees commits still in the -wal, and leaves the -shm lock index alone too."""
    live_dir = tmp_path / "live"
    live_dir.mkdir()
    live = live_dir / "live.sqlite"
    store = SQLiteActionLivenessStore(live)  # held open: its commits stay in the -wal
    store.request_pause(9, "drift")
    assert sorted(p.name for p in live_dir.iterdir()) == ["live.sqlite", "live.sqlite-shm", "live.sqlite-wal"]
    before = {p.name: p.read_bytes() for p in live_dir.iterdir()}

    copy = snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    assert {p.name: p.read_bytes() for p in live_dir.iterdir()} == before
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


def test_a_log_checkpointed_away_mid_copy_is_copied_again(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "live.sqlite"
    store = SQLiteActionLivenessStore(live)
    store.request_pause(9, "drift")
    copies: list[str] = []
    real_copy = shutil.copyfile

    def checkpoint_on_first_log_copy(src, dst):
        if str(src).endswith("-wal") and not copies:
            copies.append(str(src))
            raise FileNotFoundError(src)
        return real_copy(src, dst)

    monkeypatch.setattr("issue_orchestrator.infra.sqlite_snapshot.shutil.copyfile", checkpoint_on_first_log_copy)

    copy = snapshot_sqlite(live, tmp_path / "copy.sqlite", timeout=10.0)

    assert copies  # the first attempt was abandoned
    assert [p.issue_number for p in SQLiteActionLivenessStore(copy).pending_pauses()] == [9]


def test_a_database_that_never_holds_still_is_an_unreadable_source(
    state, tmp_path, monkeypatch
) -> None:
    def always_changing(live, destination, *, timeout):
        raise ReadOnlySqliteAccessError(ReadOnlySqliteFailure.UNREADABLE, "changed during each copy")

    monkeypatch.setattr(engine_snapshot, "snapshot_sqlite", always_changing)

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert report.partial is True
    assert {r.status for r in report.sources if r.source is not AuditSource.GITHUB and r.source is not AuditSource.LOG} == {
        SourceStatus.UNREADABLE
    }


def test_an_unresolved_record_just_short_of_the_threshold_is_not_stale(state) -> None:
    rig = Rig(state / cli.VALIDATED_WORK_DB)
    almost = NOW - STALE_UNRESOLVED_AFTER + timedelta(seconds=10)
    rig.open().admit(capture(issue=7003, branch="almost", at=almost.isoformat()))
    gc.collect()
    with tempfile.TemporaryDirectory() as scratch:
        args = cli.build_parser().parse_args(["--state-dir", str(state), "--repo", REPO, "--no-github"])
        report = audit_engine(cli._inputs(state, Path(scratch), args), now=NOW, window=timedelta(hours=24))

    stale = {a.subject for a in report.anomalies if a.kind is AnomalyKind.STALE_UNRESOLVED_WORK}
    assert "#7003" not in stale
    assert "#7001" in stale


def test_two_unreadable_claims_on_one_issue_are_two_anomalies(state, tmp_path, monkeypatch, make_session) -> None:
    db = state / cli.PENDING_WORK_CLAIMS_DB
    store = SqlitePendingWorkClaimStore(db)
    for attempt in range(2):
        session = make_session(issue_number=8, task=SessionKind.REVIEW if attempt else SessionKind.CODE)
        store.hold_pending_work_claim(
            session.run_assets,
            PendingWorkClaim(
                PendingWorkKind.REWORK,
                PendingRework(GitHubIssueKey(REPO, "8"), "agent:coder", pr_number=98),
            ),
            issue_number=8,
        )
    del store
    gc.collect()
    with closing(sqlite3.connect(db)) as conn:
        good = dict(conn.execute("SELECT run_key, payload FROM pending_work_claim WHERE issue_number = 8"))
        conn.execute("UPDATE pending_work_claim SET payload = '{corrupt' WHERE issue_number = 8")
        conn.commit()
    first = _run(state, tmp_path, monkeypatch, FakeHost())
    unreadable = {a.signature for a in first.anomalies if a.kind is AnomalyKind.UNREADABLE_CLAIM}
    assert unreadable == set(good)
    previous = tmp_path / "previous.json"
    previous.write_text(first.model_dump_json(), encoding="utf-8")
    repaired = sorted(good)[0]
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("UPDATE pending_work_claim SET payload = ? WHERE run_key = ?", (good[repaired], repaired))
        conn.commit()

    diff = _run(state, tmp_path, monkeypatch, FakeHost(), "--previous", str(previous)).diff

    assert diff is not None
    assert [a.signature for a in diff.resolved] == [repaired]
    assert [p.anomaly.signature for p in diff.persisting if p.anomaly.kind is AnomalyKind.UNREADABLE_CLAIM] == [
        sorted(good)[1]
    ]


def test_a_failed_report_install_leaves_nothing_beside_the_output(state, tmp_path, monkeypatch) -> None:
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    monkeypatch.setattr(cli, "create_repository_host", lambda repo: FakeHost())

    def refuse(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(cli.os, "replace", refuse)

    with pytest.raises(OSError, match="disk full"):
        cli.main(["--state-dir", str(state), "--repo", REPO, "--output", str(out_dir / "report.json")])

    assert list(out_dir.iterdir()) == []


def test_a_timeline_row_the_audit_cannot_decode_makes_the_timeline_unread(
    state, tmp_path, monkeypatch
) -> None:
    with closing(sqlite3.connect(state / cli.TIMELINE_DB)) as conn:
        conn.execute(
            "UPDATE timeline_events SET data_json = '{not json' WHERE issue_number = 410"
            " AND source_event = 'reconciliation.required'"
        )
        conn.commit()

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (reading,) = [r for r in report.sources if r.source is AuditSource.TIMELINE]
    assert reading.status is SourceStatus.UNREADABLE and "malformed payload" in reading.detail
    assert AnomalyKind.NO_PROGRESS_TIMELINE not in _kinds(report)


def test_a_database_that_cannot_be_copied_is_unreadable_and_leaves_no_copy(
    state, tmp_path, monkeypatch
) -> None:
    real_copy = shutil.copyfile

    def denied(src, dst):
        if str(src).endswith(cli.ACTION_LIVENESS_DB):
            real_copy(src, dst)  # a partial copy exists when the read fails
            raise PermissionError(13, "Permission denied", str(src))
        return real_copy(src, dst)

    monkeypatch.setattr("issue_orchestrator.infra.sqlite_snapshot.shutil.copyfile", denied)
    copy = tmp_path / "copy.sqlite"

    with pytest.raises(ReadOnlySqliteAccessError) as refused:
        snapshot_sqlite(state / cli.ACTION_LIVENESS_DB, copy, timeout=10.0)
    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert refused.value.reason is ReadOnlySqliteFailure.UNREADABLE
    assert not copy.exists()
    (reading,) = [r for r in report.sources if r.source is AuditSource.ACTION_LIVENESS]
    assert reading.status is SourceStatus.UNREADABLE


def test_an_unreadable_admission_instant_makes_validated_work_unread(
    state, tmp_path, monkeypatch
) -> None:
    with closing(sqlite3.connect(state / cli.VALIDATED_WORK_DB)) as conn:
        conn.execute("UPDATE validated_work_records SET created_at = 'yesterday' WHERE issue_number = 7001")
        conn.commit()

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (reading,) = [r for r in report.sources if r.source is AuditSource.VALIDATED_WORK]
    assert reading.status is SourceStatus.UNREADABLE and "created_at" in reading.detail
    assert report.validated_work is None
    assert AnomalyKind.STALE_UNRESOLVED_WORK not in _kinds(report)


def test_the_report_carries_each_subjects_last_state_change(state, tmp_path, monkeypatch) -> None:
    report = _run(state, tmp_path, monkeypatch, FakeHost())

    assert [c.subject for c in report.no_progress.state_changes] == ["#411"]


def test_a_store_database_missing_its_tables_is_unread_not_empty(state, tmp_path, monkeypatch) -> None:
    """Opening the store would create the tables, and read "no parked actions"."""
    first = _run(state, tmp_path, monkeypatch, FakeHost())
    previous = tmp_path / "previous.json"
    previous.write_text(first.model_dump_json(), encoding="utf-8")
    db = state / cli.ACTION_LIVENESS_DB
    for sidecar in state.glob(cli.ACTION_LIVENESS_DB + "*"):
        sidecar.unlink()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE unrelated (x)")
        conn.commit()

    report = _run(state, tmp_path, monkeypatch, FakeHost(), "--previous", str(previous))

    (reading,) = [r for r in report.sources if r.source is AuditSource.ACTION_LIVENESS]
    assert reading.status is SourceStatus.UNREADABLE and "action_liveness" in reading.detail
    assert report.diff is not None
    assert {a.kind for a in report.diff.unobserved} >= {AnomalyKind.PARKED_ACTION, AnomalyKind.OWED_PAUSE}
    assert all(a.kind is not AnomalyKind.PARKED_ACTION for a in report.diff.resolved)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("data_json", ""),  # empty: not "{}"
        ("timestamp", (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:61:00+00:00")),  # inside the window, not an instant
    ],
)
def test_a_damaged_timeline_row_makes_the_timeline_unread(
    state, tmp_path, monkeypatch, column, value
) -> None:
    with closing(sqlite3.connect(state / cli.TIMELINE_DB)) as conn:
        conn.execute(
            f"UPDATE timeline_events SET {column} = ? WHERE sequence = ("
            "SELECT MIN(sequence) FROM timeline_events WHERE issue_number = 411"
            " AND source_event = 'issue.labels_changed')",
            (value,),
        )
        if column == "data_json":
            conn.execute(
                "UPDATE timeline_events SET data_json = '' WHERE issue_number = 410"
                " AND source_event = 'reconciliation.required'"
            )
        conn.commit()

    report = _run(state, tmp_path, monkeypatch, FakeHost())

    (reading,) = [r for r in report.sources if r.source is AuditSource.TIMELINE]
    assert reading.status is SourceStatus.UNREADABLE
    assert AnomalyKind.NO_PROGRESS_TIMELINE not in _kinds(report)


@pytest.mark.parametrize("vanishes", ["before_stat", "before_open"])
def test_a_log_rotated_away_mid_audit_is_an_unread_source(state, tmp_path, monkeypatch, vanishes) -> None:
    """The engine's TimedRotatingFileHandler renames the log at midnight."""
    first = _run(state, tmp_path, monkeypatch, FakeHost())
    previous = tmp_path / "previous.json"
    previous.write_text(first.model_dump_json(), encoding="utf-8")
    log = state / cli.ENGINE_LOG
    real_excerpt = engine_log_reader.log_excerpt

    def rotate_then(path, *, tail_bytes):
        if vanishes == "before_stat":
            log.rename(log.with_name("orchestrator.log.2026-09-27"))
            return real_excerpt(path, tail_bytes=tail_bytes)
        excerpt = real_excerpt(path, tail_bytes=tail_bytes)
        log.rename(log.with_name("orchestrator.log.2026-09-27"))
        return excerpt

    monkeypatch.setattr(engine_log_reader, "log_excerpt", rotate_then)

    report = _run(state, tmp_path, monkeypatch, FakeHost(), "--previous", str(previous))

    (reading,) = [r for r in report.sources if r.source is AuditSource.LOG]
    assert reading.status is SourceStatus.UNREADABLE
    assert report.diff is not None
    assert AnomalyKind.FETCH_COST_INVERTED in {a.kind for a in report.diff.unobserved}
    assert all(a.sources[0] is not AuditSource.LOG for a in report.diff.resolved)


def test_a_database_that_changes_under_every_copy_is_unreadable(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "live.sqlite"
    with closing(sqlite3.connect(live)) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.commit()
    stamps = iter(range(100))
    monkeypatch.setattr(
        "issue_orchestrator.infra.sqlite_snapshot._identity", lambda _path, **_kwargs: next(stamps)
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
        list(SqliteTimelineAuditReader(db, timeout=10.0).events_between(NOW - timedelta(days=1), NOW))

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
        repo=REPO, state_dir=Path("/nowhere"), blocked_lane=BlockedLane.of(None),
        validated_work=absent, action_liveness=absent,
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
