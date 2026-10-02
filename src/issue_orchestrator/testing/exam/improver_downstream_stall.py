"""Improver exam case IM2: the work a block holds up (#7490 step 4).

IM1 grades the improver on accounting for every blocked item. This case
grades it on looking DOWNSTREAM of one: is the item's own published work
stuck too?

The fixture is the shape porchpin showed on 2026-10-02 (engine 387f107):

* **#364** is blocked (``needs-human``: the coder asked the maintainer to
  decide a reviewer's point). The tech lead diagnosed the question.
* Its PR **#379** carries the validated work recovery had just published.
  Recovery re-queued its code review at 11:38:29Z; the launcher dropped it
  ("Dropping stale pending review ... reason=issue_blocked") and from then on
  the PR scanner found it and skipped it on EVERY scan ("Skipping stale review
  PR ... reason=issue_blocked"), as it had for six hours before the restart.
  The published work can never be reviewed while the issue is blocked.

The engine logs each of those decisions at INFO. The improver's blind run
``20261002T120316Z-1b2115c1`` (started 25 minutes into the veto) graded #364
only as "the tech lead did not escalate the agent's question": its audit
counted ERROR/WARNING lines only, and ``blocked-items.json`` said nothing
about the item's PR, so the livelock downstream of the block was invisible.

Unlike IM1, the staged ``improver-data/`` is not hand-written: it is the
REAL audit and blocked-items assembly run over the raw records (the engine's
log lines, its timeline rows, its open issues and PRs), so the case fails
whenever the inputs stop showing the veto, not only when the validator does.
The right answer keys the refused review (``refused_work`` of ``PR #379``)
in a finding that cites its snapshot, live and recurring after the start,
and accounts for it in #364's ``downstream`` with the impact and the PR's
own pipeline event.

:func:`build_case` writes the fixture; :func:`grade` grades a findings file
the validator ACCEPTED. The deterministic half runs in the validation gate;
the live half runs the real improver model on it (``make test-improver-exam``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from ...contracts.engine_audit import AnomalyKind, EngineAuditReport, SourceStatus
from ...contracts.engine_start import EffectiveCharter
from ...contracts.improver_findings import ImproverFindings
from ...contracts.improver_inputs import (
    AUDIT_FILE,
    BLOCKED_ITEMS_FILE,
    CASE_FILES_FILE,
    CHARTER_DECISIONS_FILE,
    CHARTER_FILE,
    ENGINE_SOURCE_DIRNAME,
    ENGINE_START_FILE,
    EXAM_DIRNAME,
    IMPROVER_DATA_DIRNAME,
    INPUTS_FILE,
    OPEN_ISSUES_FILE,
    BlockedItemsInput,
    CaseFilesInput,
    CharterDecisionsInput,
    Coverage,
    EngineStartInput,
    InputsManifest,
    OpenIssue,
    OpenIssuesInput,
    StagedDiagnosis,
    StagedInput,
    to_json,
)
from ...infra.config import Config
from ...infra.engine_log_reader import EngineLogEntry, EngineLogExcerpt
from ...observation.engine_audit import (
    BlockedLane,
    EngineAuditInputs,
    EngineLog,
    Unavailable,
    audit_engine,
)
from ...observation.improver_blocked_items import blocked_items_input
from ...ports.engine_audit import OpenIssueLabels, TimelineEvent
from ...ports.pending_work_claim_store import NeedsHumanCauseRow
from ...ports.pull_request_tracker import PRInfo
from ...ports.timeline_store import TimelineRecord
from .cases import EXAM_CASE_IDS

CASE_ID = "IM2-blocked-item-review-vetoed"

ENGINE_ID = "repo-89afeb99eff0542e787f28211c93f58cd90aa3e0fe9d2bf3acd6fcdc63e2fb73"
AUDITED_REPO = "porchpin/porchpin"
OUTPUTS_REPO = "issue-orchestrator/issue-orchestrator"
STARTED = datetime.fromisoformat("2026-10-02T11:32:09+00:00")
CUTOFF = datetime.fromisoformat("2026-10-02T12:03:16+00:00")
WINDOW = timedelta(hours=24)

ISSUE = 364
PR = 379
#: The anomaly the right answer keys: the review the engine keeps refusing.
VETO_KEY = (AnomalyKind.REFUSED_WORK.value, f"PR #{PR}", "review:issue_blocked")
#: The health review that diagnosed #364's question, and nothing more.
DIAGNOSIS_364 = "tech-lead-run:20261002-113951Z-ef91ccbe76c84015aa63851d3224fa9a:tech-lead-1"

_BRANCH = "364-cuj-r2-h1-hold-the-batch-s-delivery-owner-provenan"
_ISSUE_LABELS = ("agent:backend", "needs-human", "pr-pending", "priority:medium", "v1")
_LABEL_TEXT = (
    "issue_labels=v1,needs-human,priority:medium,agent:backend,pr-pending"
    " pr_labels=tech-lead-reviewed,needs-code-review,rework-cycle-5"
)
_SCANNER = "issue_orchestrator.control.pr_scanner"
_SCANNER_SKIP = f"[SCANNER] Skipping stale review PR: pr={PR} issue={ISSUE} reason=issue_blocked {_LABEL_TEXT}"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- the raw records -------------------------------------------------------------


def _entry(at: datetime, logger: str, message: str, level: str = "INFO") -> EngineLogEntry:
    return EngineLogEntry(at=at, level=level, logger=logger, message=message)


def log_entries() -> tuple[EngineLogEntry, ...]:
    """What the engine logged, oldest first: the scanner's skip every few
    minutes for six hours, the restart, the re-queue dropped at launch, and
    the skip again on every scan after it. All at INFO."""
    before_restart = [
        _entry(_at("2026-10-02T05:30:00+00:00") + timedelta(minutes=15 * n), _SCANNER, _SCANNER_SKIP)
        for n in range(24)
    ]
    after_requeue = [
        _entry(_at(f"2026-10-02T{hms}+00:00"), _SCANNER, _SCANNER_SKIP)
        for hms in (
            "11:40:48", "11:43:13", "11:46:00", "11:48:30", "11:51:00",
            "11:53:46", "11:56:13", "11:59:14", "12:01:48",
        )
    ]
    return (
        # Before the window: the read reaches back over all of it.
        _entry(CUTOFF - WINDOW - timedelta(hours=1), "issue_orchestrator.control.orchestrator_support",
               "[LOOP] Iteration 1 - active=0"),
        *before_restart,
        _entry(_at("2026-10-02T11:32:15+00:00"), "issue_orchestrator.control.startup_manager",
               f"[startup] Dropping stale pending review recovery: pr={PR} issue={ISSUE} reason=issue_blocked"
               f" {_LABEL_TEXT}"),
        _entry(_at("2026-10-02T11:38:55+00:00"), "issue_orchestrator.control.transition_log",
               f"[TRANSITION] review #{PR}: QUEUED → SKIP (stale pending review: issue_blocked)"),
        _entry(_at("2026-10-02T11:38:55+00:00"), "issue_orchestrator.control.session_review_support",
               f"[launch] Dropping stale pending review: pr={PR} issue={ISSUE} reason=issue_blocked {_LABEL_TEXT}"),
        *after_requeue,
    )


def _event(at: str, name: str, **data: object) -> TimelineEvent:
    return TimelineEvent(
        issue_number=ISSUE,
        record=TimelineRecord(
            event_id=f"{name}-{at}", timestamp=at, event=name,
            data={"issue_number": ISSUE, **data}, source_event=name,
        ),
    )


def timeline() -> tuple[TimelineEvent, ...]:
    """#364's timeline rows, oldest first."""
    return (
        _event("2026-10-02T05:05:22+00:00", "issue.labels_changed", added=["recovery-pending"], removed=[]),
        _event("2026-10-02T05:24:57+00:00", "issue.labels_changed", added=["needs-human"], removed=[]),
        _event("2026-10-02T11:37:45+00:00", "issue.labels_changed", added=[], removed=["recovery-pending"]),
        _event("2026-10-02T11:38:29+00:00", "pr.view_changed", pr_number=PR,
               added=["needs-code-review"], removed=[]),
        _event("2026-10-02T11:38:29.400000+00:00", "review.queued", pr_number=PR),
        _event("2026-10-02T11:38:55+00:00", "review.skipped", pr_number=PR,
               reason="stale_pending_review:issue_blocked"),
    )


def open_issues_of_engine() -> tuple[OpenIssueLabels, ...]:
    return (
        OpenIssueLabels(number=ISSUE, title="[CUJ:R2,H1] Hold the batch's Delivery-owner provenance",
                        labels=_ISSUE_LABELS),
        OpenIssueLabels(number=440, title="[CUJ:S2] Seller payout export", labels=("agent:backend", "v1")),
    )


def open_prs_of_engine() -> tuple[PRInfo, ...]:
    return (
        PRInfo(number=PR, title="Hold the batch's Delivery-owner provenance", url=f"https://github.com/{AUDITED_REPO}/pull/{PR}",
               branch=_BRANCH, body=f"Closes #{ISSUE}", state="open",
               labels=["tech-lead-reviewed", "needs-code-review", "rework-cycle-5"], draft=False),
    )


# -- the real assembly over them ------------------------------------------------------


@dataclass(frozen=True)
class _Timeline:
    events: tuple[TimelineEvent, ...]

    def events_between(self, start: datetime, end: datetime) -> Iterable[TimelineEvent]:
        return [e for e in self.events if start <= datetime.fromisoformat(e.record.timestamp) <= end]


@dataclass(frozen=True)
class _Host:
    def list_open_issue_labels_complete(self) -> Sequence[OpenIssueLabels]:
        return open_issues_of_engine()

    def list_open_prs_complete(self) -> Sequence[PRInfo]:
        return open_prs_of_engine()


def _log() -> tuple[EngineLogExcerpt, Iterator[EngineLogEntry]]:
    return EngineLogExcerpt(path=Path("/state/logs/orchestrator.log"), size=1, bytes_read=1, truncated=False), iter(
        log_entries()
    )


def _lane() -> BlockedLane:
    return BlockedLane.of(BlockedLane.policy_of(Config()))


def audit() -> EngineAuditReport:
    """The audit the improver is given: :func:`audit_engine` over the raw records.

    The stores this case is not about are absent, as on an engine that never
    created them; the log, the timeline and GitHub are read."""
    absent = Unavailable(SourceStatus.ABSENT, "not part of this case")
    return audit_engine(
        EngineAuditInputs(
            repo=AUDITED_REPO, state_dir=Path("/state"), blocked_lane=_lane(),
            validated_work=absent, action_liveness=absent, tech_lead=absent, claims=absent,
            timeline=_Timeline(timeline()), log=EngineLog(read=_log), github=_Host(),
        ),
        now=CUTOFF,
        window=WINDOW,
    )


def _unproven(start: datetime | None, what: str) -> Coverage:
    return Coverage(
        from_=start, to=CUTOFF, complete=False,
        detail=f"{what}: not proven: a record dated before the cutoff can commit after the copy (#7525)",
    )


def case_files() -> CaseFilesInput:
    return CaseFilesInput(
        coverage=_unproven(CUTOFF - WINDOW, "the case-file ledger"),
        case_files=(),
        diagnoses_coverage=Coverage(from_=None, to=CUTOFF, complete=False, detail="best-effort run history"),
        diagnoses=(
            StagedDiagnosis(
                id=DIAGNOSIS_364, run_key="global:health_review", scope_kind="global_health_review",
                flavor="health_review", phase="completed",
                started_at=_at("2026-10-02T11:39:51+00:00"), ended_at=_at("2026-10-02T11:57:40+00:00"),
                subject_issue_number=0, subject_title="", anchor_issue_number=439,
                body="T1: #364 waits on the maintainer: the coder asked whether Delivery-owned provenance"
                " may be held or must be ruled unholdable (reviewer A1). Advice: answer A1 on #364,"
                " then remove needs-human.",
                findings=4, proposals=0,
            ),
        ),
    )


def blocked_items(report: EngineAuditReport) -> BlockedItemsInput:
    """``blocked-items.json`` as staging assembles it, from the same records."""
    return blocked_items_input(
        open_issues_of_engine(),
        prs=open_prs_of_engine(),
        audit=report,
        lane=_lane(),
        causes=(NeedsHumanCauseRow(ISSUE, "agent_completion", "agent requested needs_human on completion"),),
        ledger=(),
        case_files=case_files(),
        timeline=timeline(),
        cutoff=CUTOFF,
        coverage_proven=False,
    )


def open_issues() -> OpenIssuesInput:
    """io's open issues, WITHOUT the ones tracking the veto (#7593 and its
    kin): the improver must find it unaided."""
    return OpenIssuesInput(
        repo=OUTPUTS_REPO, read_at=CUTOFF,
        issues=(
            OpenIssue(number=7490, title="Tech-lead improver (second-order tech lead)", labels=("enhancement",)),
            OpenIssue(number=7491, title="Incremental refresh costs more GitHub calls than a full one", labels=("bug",)),
        ),
    )


def build_case(
    root: Path,
    *,
    engine_commit: str,
    charter: EffectiveCharter,
    export_source: Callable[[Path], None],
) -> Path:
    """Write the case's ``improver-data/`` under ``root`` and return it.

    ``export_source`` fills ``engine-source/`` (the io tree at
    ``engine_commit`` for a live run; any tree for the validator-level test).
    """
    data = root / IMPROVER_DATA_DIRNAME
    data.mkdir(parents=True)

    def write(name: str, document: BaseModel) -> None:
        (data / name).write_text(to_json(document), encoding="utf-8")

    report = audit()
    write(AUDIT_FILE, report)
    write(ENGINE_START_FILE, EngineStartInput(
        started_at=STARTED, engine_commit=engine_commit, package_version="0.10.0", repo_head=None,
    ))
    write(CHARTER_FILE, charter)
    write(CHARTER_DECISIONS_FILE, CharterDecisionsInput(
        coverage=_unproven(CUTOFF - WINDOW, "the charter ledger"), decisions=(),
    ))
    write(CASE_FILES_FILE, case_files())
    write(BLOCKED_ITEMS_FILE, blocked_items(report))
    write(OPEN_ISSUES_FILE, open_issues())
    (data / EXAM_DIRNAME).mkdir()
    export_source(data / ENGINE_SOURCE_DIRNAME)
    write(INPUTS_FILE, InputsManifest(
        staged_at=CUTOFF, engine_id=ENGINE_ID, audited_repo=AUDITED_REPO, outputs_repo=OUTPUTS_REPO,
        inputs=tuple(
            StagedInput(name=name, staged=True, detail="")
            for name in (
                ENGINE_START_FILE, CHARTER_FILE, AUDIT_FILE, CHARTER_DECISIONS_FILE, CASE_FILES_FILE,
                BLOCKED_ITEMS_FILE, OPEN_ISSUES_FILE, ENGINE_SOURCE_DIRNAME,
            )
        ) + tuple(
            StagedInput(name=name, staged=False, detail="no previous improver run")
            for name in ("audit-previous.json", "audit-diff.json")
        ) + (StagedInput(name=EXAM_DIRNAME, staged=False, detail="no scorecards"),),
        existing_exam_case_ids=EXAM_CASE_IDS,
        exam_scores_comparable=False,
    ))
    return data


# -- the grade -------------------------------------------------------------------


@dataclass(frozen=True)
class ImproverExamGrade:
    case_id: str
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


def grade(findings: ImproverFindings) -> ImproverExamGrade:
    """Grade an ACCEPTED findings file (the validator already enforced that a
    finding keys the refused review, and that every citation resolves)."""
    failures: list[str] = []
    downstream = [
        d for a in findings.blocked_items if a.number == ISSUE for d in a.downstream if d.anomaly_key.key == VETO_KEY
    ]
    if not any(d.pipeline_event is not None for d in downstream):
        failures.append(f"#{ISSUE}'s account does not name PR #{PR}'s refused review with its pipeline event")
    about = [f for f in findings.findings if VETO_KEY in {k.key for k in f.anomaly_keys}]
    if not about:
        failures.append(f"PR #{PR}'s review, refused on every scan while #{ISSUE} is blocked, has no finding")
    elif not any(f.present_after_start == "true" and f.recurs_after_start == "true" for f in about):
        failures.append(
            f"PR #{PR}'s refused review is live: present after the start and refused again after it"
        )
    if not any(a.number == ISSUE for a in findings.blocked_items):
        failures.append(f"#{ISSUE} is not accounted for")
    return ImproverExamGrade(case_id=CASE_ID, failures=tuple(failures))


__all__ = [
    "AUDITED_REPO",
    "CASE_ID",
    "CUTOFF",
    "DIAGNOSIS_364",
    "ENGINE_ID",
    "ImproverExamGrade",
    "STARTED",
    "VETO_KEY",
    "audit",
    "blocked_items",
    "build_case",
    "grade",
    "log_entries",
    "timeline",
]
