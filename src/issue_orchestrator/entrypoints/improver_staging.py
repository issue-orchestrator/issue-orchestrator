"""Stage the improver's inputs: ``$ISSUE_ORCHESTRATOR_RUN_DIR/improver-data/`` (#7490).

The one owner of what the improver reads. It writes exactly the prompt's
Inputs table (``examples/prompts/tech-lead-improver.md``), each file a typed
document (:mod:`..contracts.improver_inputs`), plus ``inputs.json`` saying what
was staged and why anything was not.

It never opens a live engine database: every store is read from a byte copy
(:mod:`.engine_snapshot`), one copy per store, taken once and shared by the
audit and every other reading of that store. The audited engine's state is
only ever read.

An input the improver cannot work without (the engine's start record, its
source, the open issues its outputs are deduplicated against) makes staging
fail with :class:`ImproverInputsUnavailable`; any other missing source is
recorded as missing, and the improver is told to draw no conclusion from it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from ..contracts.engine_audit import AuditDiff, AuditSource, EngineAuditReport, SourceStatus
from ..contracts.engine_start import EffectiveCharter
from ..contracts.improver_inputs import (
    AUDIT_DIFF_FILE,
    AUDIT_FILE,
    AUDIT_PREVIOUS_FILE,
    BLOCKED_ITEMS_FILE,
    CASE_FILES_FILE,
    CHARTER_DECISIONS_FILE,
    CHARTER_FILE,
    ENGINE_SOURCE_DIRNAME,
    ENGINE_START_FILE,
    EXAM_DIRNAME,
    IMPROVER_DATA_DIRNAME,
    INPUTS_FILE,
    INTERVENTIONS_FILE,
    OPEN_ISSUES_FILE,
    PREVIOUS_SCORECARD_SUFFIX,
    BlockedItemsInput,
    CaseFilesInput,
    CharterDecisionsInput,
    EngineStartInput,
    InputsManifest,
    InterventionsInput,
    OpenIssue,
    OpenIssuesInput,
    ScorecardHead,
    StagedInput,
    to_json,
)
from ..domain.engine_activity import EngineRef
from ..domain.improver_findings_validation import StagedEvidence, parse_documents
from ..domain.read_only_sqlite import ReadOnlySqliteAccessError
from ..infra.engine_start_record import EngineStartRecordUnavailable, read_engine_start
from ..infra.tech_lead_run_record_store import SqliteTechLeadRunRecordStore
from ..infra.pause_journal import PAUSE_JOURNAL_FILENAME, JsonlPauseJournal
from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
from ..observation.engine_audit import Unavailable, audit_engine
from ..observation.improver_blocked_items import blocked_items_input
from ..observation.engine_audit_diff import IncomparableAuditError, diff_reports, load_report
from ..observation.improver_inputs import (
    ExamSeries,
    Scorecard,
    case_files_input,
    charter_decisions_input,
    engine_start_input,
    exam_series,
    interventions_input,
)
from ..ports.engine_audit import OpenIssueLabels, OpenWorkHost, TechLeadRunHistoryRead
from ..ports.pull_request_tracker import PRInfo
from ..testing.exam.cases import EXAM_CASE_IDS
from ..execution.improver_citations import RunDirCitations
from ..execution.improver_engine_source import RunDirEngineSource
from ..ports.operator_activity import OperatorActivitySource, RepoActivityRead
from .engine_snapshot import EngineSnapshot, snapshot_engine, snapshot_tech_lead_runs

M = TypeVar("M", bound=BaseModel)

#: Pause-journal rows kept by the journal itself; all of them are read.
_PAUSE_ROWS = 500


class ImproverInputsUnavailable(RuntimeError):
    """An input the improver cannot run without could not be staged."""


class EngineSourceExporter(Protocol):
    def export(self, commit: str, destination: Path) -> None: ...


class OpenIssueListing(Protocol):
    """The outputs repository's open issues (``OpenWorkHost``'s issue half)."""

    def list_open_issue_labels_complete(self) -> Sequence[OpenIssueLabels]: ...


@dataclass(frozen=True)
class ImproverStagingRequest:
    #: The audited engine: its key, its repository and its state directory
    #: (read only, through snapshots).
    engine: EngineRef
    #: Where the improver's outputs are filed (io's own repository).
    outputs_repo: str
    #: ``$ISSUE_ORCHESTRATOR_RUN_DIR``; ``improver-data/`` must not exist yet.
    run_dir: Path
    previous_audit: Path | None
    exam_dir: Path | None
    window: timedelta
    log_tail_bytes: int
    #: Open issues of the outputs repository left out of ``open-issues.json``
    #: for a blind run (the improver must find what they track unaided).
    excluded_open_issues: frozenset[int] = frozenset()


@dataclass(frozen=True)
class StagedImproverInputs:
    data_dir: Path
    manifest: InputsManifest
    audit: EngineAuditReport


class ImproverInputStager:
    def __init__(
        self,
        *,
        audited_host: OpenWorkHost | Unavailable,
        outputs_host: OpenIssueListing,
        source: EngineSourceExporter,
        activity: OperatorActivitySource | Unavailable,
        clock: Callable[[], datetime],
    ) -> None:
        self._audited_host = audited_host
        self._outputs_host = outputs_host
        self._source = source
        self._activity = activity
        self._clock = clock

    def stage(self, request: ImproverStagingRequest) -> StagedImproverInputs:
        # The cutoff is taken BEFORE any store is copied, so every record
        # written by the cutoff is in the copies. A copy may also hold later
        # writes; each source is projected back to the cutoff by its own
        # timestamps (decisions by ``as_of``, case files by their
        # observations, runs by ``run_as_of``), and the log and timeline are
        # read to the cutoff. Taking it after the copy instead would claim
        # coverage over writes the copy missed.
        now = self._clock()
        data = request.run_dir / IMPROVER_DATA_DIRNAME
        data.mkdir(parents=True, exist_ok=False)
        entries: list[StagedInput] = []
        try:
            record = read_engine_start(request.engine.state_dir)
            start = engine_start_input(record)
        except (EngineStartRecordUnavailable, ValueError) as error:
            raise ImproverInputsUnavailable(str(error)) from error
        if record.repo != request.engine.repo:
            # The state was written by an engine working another repository
            # (its config was re-pointed since): never attribute it to this one.
            raise ImproverInputsUnavailable(
                f"the engine at {request.engine.state_dir} started for {record.repo},"
                f" not {request.engine.repo}"
            )
        _write(data / ENGINE_START_FILE, start)
        _write(data / CHARTER_FILE, record.charter)
        entries += [_staged(ENGINE_START_FILE), _staged(CHARTER_FILE, "effective at the latest start")]
        audited = _OnceListing(self._audited_host)
        with tempfile.TemporaryDirectory(prefix="io-improver-") as scratch:
            snapshot = snapshot_engine(
                request.engine.state_dir,
                Path(scratch),
                repo=request.engine.repo,
                log_tail_bytes=request.log_tail_bytes,
                github=audited.as_host(),
            )
            runs_store = snapshot_tech_lead_runs(request.engine.state_dir, Path(scratch))
            audit = audit_engine(snapshot.audit, now=now, window=request.window)
            audit, previous_entries = _with_previous(audit, request.previous_audit, data)
            _write(data / AUDIT_FILE, audit)
            entries += [_staged(AUDIT_FILE, "partial" if audit.partial else "all sources read")]
            entries += previous_entries
            window_start = now - request.window
            tech_lead = _stage_tech_lead(snapshot, runs_store, request, data, window_start, now)
            entries += tech_lead.entries
            entries.append(self._stage_interventions(tech_lead.ledger, snapshot, request, data, window_start, now))
            entries.append(_stage_blocked_items(audit, audited, snapshot, tech_lead, data, now))
        entries.append(self._stage_open_issues(request, audited, data, now))
        series, unreadable_cards = _stage_exam(request.exam_dir, data)
        entries.append(_exam_entry(series, request.exam_dir, unreadable_cards))
        try:
            self._source.export(start.engine_commit, data / ENGINE_SOURCE_DIRNAME)
        except Exception as error:
            raise ImproverInputsUnavailable(f"engine source: {error}") from error
        entries.append(_staged(ENGINE_SOURCE_DIRNAME, f"io at {start.engine_commit}"))
        manifest = InputsManifest(
            staged_at=now,
            engine_id=request.engine.engine_id,
            audited_repo=request.engine.repo,
            outputs_repo=request.outputs_repo,
            inputs=tuple(entries),
            existing_exam_case_ids=tuple(sorted({*EXAM_CASE_IDS, *series.latest, *series.previous})),
            exam_scores_comparable=series.comparable and not unreadable_cards,
        )
        _write(data / INPUTS_FILE, manifest)
        return StagedImproverInputs(data_dir=data, manifest=manifest, audit=audit)

    def _stage_interventions(
        self,
        ledger: tuple[TechLeadCharterDecision, ...] | str,
        snapshot: EngineSnapshot,
        request: ImproverStagingRequest,
        data: Path,
        window_start: datetime,
        cutoff: datetime,
    ) -> StagedInput:
        """The operator's interventions: the engine's records and the hand
        actions on the audited repository's GitHub (#8001). Each source that
        cannot be read is named in the file; none stops the others."""
        timeline = snapshot.timeline
        interventions = interventions_input(
            ledger,
            f"{timeline.status.value}: {timeline.detail}"
            if isinstance(timeline, Unavailable)
            else timeline.events_between(window_start, cutoff),
            JsonlPauseJournal(request.engine.state_dir / PAUSE_JOURNAL_FILENAME).recent(limit=_PAUSE_ROWS),
            self._read_activity(window_start, cutoff),
            repo=request.engine.repo,
            window_start=window_start,
            cutoff=cutoff,
        )
        _write(data / INTERVENTIONS_FILE, interventions)
        return _staged(INTERVENTIONS_FILE, f"a floor: not every intervention is recorded; GitHub {interventions.github.detail}")

    def _read_activity(self, since: datetime, until: datetime) -> RepoActivityRead | str:
        if isinstance(self._activity, Unavailable):
            return f"{self._activity.status.value}: {self._activity.detail}"
        try:
            return self._activity.read(since=since, until=until)
        except Exception as error:  # an unreadable source is named, never fatal
            return f"unreadable: {type(error).__name__}: {error}"

    def _stage_open_issues(
        self,
        request: ImproverStagingRequest,
        audited: "_OnceListing",
        data: Path,
        now: datetime,
    ) -> StagedInput:
        # The audit's own listing when it read the same repository; the
        # outputs host otherwise (another repository, or the audit skipped
        # GitHub: open issues are needed either way).
        shared = audited.as_listing() if request.outputs_repo == request.engine.repo else None
        listing: OpenIssueListing = shared or self._outputs_host
        try:
            issues = listing.list_open_issue_labels_complete()
        except Exception as error:
            raise ImproverInputsUnavailable(
                f"open issues of {request.outputs_repo}: {error}"
            ) from error
        hidden = request.excluded_open_issues
        kept = [i for i in issues if i.number not in hidden]
        _write(
            data / OPEN_ISSUES_FILE,
            OpenIssuesInput(
                repo=request.outputs_repo,
                read_at=now,
                issues=tuple(
                    OpenIssue(number=i.number, title=i.title, labels=i.labels)
                    for i in sorted(kept, key=lambda i: i.number)
                ),
            ),
        )
        # The improver is not told which: naming them would point it at them.
        return _staged(OPEN_ISSUES_FILE, f"{len(kept)} open in {request.outputs_repo}")


def _with_previous(
    audit: EngineAuditReport, previous: Path | None, data: Path
) -> tuple[EngineAuditReport, list[StagedInput]]:
    if previous is None:
        missing = "no previous improver run"
        return audit, [_missing(AUDIT_PREVIOUS_FILE, missing), _missing(AUDIT_DIFF_FILE, missing)]
    try:
        earlier = load_report(previous)
        diff = diff_reports(earlier, audit)
    except IncomparableAuditError as error:
        return audit, [_missing(AUDIT_PREVIOUS_FILE, str(error)), _missing(AUDIT_DIFF_FILE, str(error))]
    _write(data / AUDIT_PREVIOUS_FILE, earlier)
    _write(data / AUDIT_DIFF_FILE, diff)
    return audit.model_copy(update={"diff": diff}), [
        _staged(AUDIT_PREVIOUS_FILE, f"generated {earlier.generated_at}"),
        _staged(AUDIT_DIFF_FILE),
    ]


@dataclass(frozen=True)
class _TechLeadStaged:
    """What staging the tech lead's records wrote, and what it read: the
    ledger (or why it could not be read) and the staged case files, which
    ``blocked-items.json`` reads again rather than re-reading the store."""

    entries: list[StagedInput]
    ledger: tuple[TechLeadCharterDecision, ...] | str
    case_files: CaseFilesInput | None


def _stage_tech_lead(
    snapshot: EngineSnapshot,
    runs_store: SqliteTechLeadRunRecordStore | Unavailable,
    request: ImproverStagingRequest,
    data: Path,
    window_start: datetime,
    cutoff: datetime,
) -> _TechLeadStaged:
    store = snapshot.tech_lead
    if isinstance(store, Unavailable):
        why = f"tech-lead authority store {store.status.value}: {store.detail}"
        return _TechLeadStaged(_tech_lead_missing(why), why, None)
    try:
        ledger = store.charter_ledger.list_all()
        case_files = store.list_case_file_records()
    except (sqlite3.Error, ReadOnlySqliteAccessError) as error:
        why = f"tech-lead authority store unreadable: {error}"
        return _TechLeadStaged(_tech_lead_missing(why), why, None)
    # A live engine's ledgers cannot yet prove that every record dated by the
    # cutoff had committed when they were copied (#7525).
    decisions = charter_decisions_input(
        ledger, window_start=window_start, cutoff=cutoff, coverage_proven=False
    )
    _write(data / CHARTER_DECISIONS_FILE, decisions)
    entries = [_staged(CHARTER_DECISIONS_FILE, decisions.coverage.detail)]
    # The case-file ledger stands on its own; an unreadable run history only
    # leaves the diagnoses (never complete anyway) empty, and says why.
    runs = (
        TechLeadRunHistoryRead(records=(), unreadable=0)
        if isinstance(runs_store, Unavailable)
        else runs_store.all_runs()
    )
    staged = case_files_input(
        case_files, runs, window_start=window_start, cutoff=cutoff, coverage_proven=False
    )
    if isinstance(runs_store, Unavailable):
        staged = staged.model_copy(update={"diagnoses_coverage": staged.diagnoses_coverage.model_copy(
            update={"detail": f"tech-lead run history {runs_store.status.value}: {runs_store.detail}"}
        )})
    _write(data / CASE_FILES_FILE, staged)
    entries.append(_staged(CASE_FILES_FILE, staged.coverage.detail))
    return _TechLeadStaged(entries, ledger, staged)


def _tech_lead_missing(why: str) -> list[StagedInput]:
    return [_missing(CHARTER_DECISIONS_FILE, why), _missing(CASE_FILES_FILE, why)]


def _stage_blocked_items(
    audit: EngineAuditReport,
    audited: "_OnceListing",
    snapshot: EngineSnapshot,
    tech_lead: _TechLeadStaged,
    data: Path,
    cutoff: datetime,
) -> StagedInput:
    """``blocked-items.json`` from the SAME open-issue listing the audit read
    (no second GitHub walk), so every blocked item it names is an attention
    anomaly of the audit, and the other stores' snapshot copies."""
    if audit.github is None:
        reading = next(r for r in audit.sources if r.source is AuditSource.GITHUB)
        why = f"the audited repository's open issues were not read ({reading.status.value}: {reading.detail})"
        if reading.status is not SourceStatus.SKIPPED:
            # The objective cannot be measured: a run that accepted findings
            # now would account for no blocked item and drop every live one.
            raise ImproverInputsUnavailable(f"{BLOCKED_ITEMS_FILE}: {why}")
        # The operator chose not to read GitHub (--no-github): said, not hidden.
        return _missing(BLOCKED_ITEMS_FILE, why)
    issues = audited.issues()
    claims = snapshot.claims
    causes = (
        f"claim store {claims.status.value}: {claims.detail}"
        if isinstance(claims, Unavailable)
        else _read_or_why(claims.list_needs_human_causes, "claim store")
    )
    timeline = snapshot.timeline
    lane = snapshot.audit.blocked_lane
    blocked = [i.number for i in issues if lane.blocking(i.labels)]
    events = (
        f"{timeline.status.value}: {timeline.detail}"
        if isinstance(timeline, Unavailable)
        else _read_or_why(lambda: tuple(timeline.issue_events(blocked, cutoff)), "timeline")
    )
    staged = blocked_items_input(
        issues,
        # The audit's own read of the open PRs (``audit.github`` is set, so it
        # read them with the issues): no second GitHub walk.
        prs=audited.prs(),
        audit=audit,
        lane=lane,
        causes=causes,
        ledger=tech_lead.ledger,
        case_files=tech_lead.case_files,
        timeline=events,
        cutoff=cutoff,
        # As for charter-decisions.json: not provable on a live engine (#7525).
        coverage_proven=False,
    )
    _write(data / BLOCKED_ITEMS_FILE, staged)
    return _staged(BLOCKED_ITEMS_FILE, f"{len(staged.items)} blocked item(s)")


R = TypeVar("R")


def _read_or_why(read: Callable[[], R], what: str) -> R | str:
    """``read()``, or why the snapshot copy could not be read."""
    try:
        return read()
    except (sqlite3.Error, ReadOnlySqliteAccessError) as error:
        return f"{what} unreadable: {error}"


def _stage_exam(exam_dir: Path | None, data: Path) -> tuple[ExamSeries, tuple[str, ...]]:
    """The staged series, and the scorecard files that could not be read.

    A scorecard is written in place, so one being written (or cut short by a
    crash) is unreadable. It is left out and named rather than failing every
    run on it; with any left out, no exam trend is comparable.
    """
    cards, unreadable = ([], ()) if exam_dir is None or not exam_dir.is_dir() else _scorecards(exam_dir)
    series = exam_series(cards)
    target = data / EXAM_DIRNAME
    target.mkdir()
    for case_id, card in series.latest.items():
        (target / f"{case_id}.json").write_text(card.text, encoding="utf-8")
    for case_id, card in series.previous.items():
        (target / f"{case_id}{PREVIOUS_SCORECARD_SUFFIX}").write_text(card.text, encoding="utf-8")
    return series, unreadable


def _scorecards(exam_dir: Path) -> tuple[list[Scorecard], tuple[str, ...]]:
    """Every scorecard the exam wrote (``<case>-<sha>-<time>.json``), not its
    raw observations (``*.observation.json``), and the ones not readable."""
    cards = []
    unreadable = []
    for path in sorted(exam_dir.glob("*.json")):
        if path.name.endswith(".observation.json"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            head = ScorecardHead.model_validate(json.loads(text))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError):
            unreadable.append(path.name)
            continue
        cards.append(
            Scorecard(
                written_at=datetime.fromtimestamp(os.stat(path).st_mtime, tz=UTC),
                head=head,
                text=text,
            )
        )
    return cards, tuple(unreadable)


def _exam_entry(series: ExamSeries, exam_dir: Path | None, unreadable: tuple[str, ...]) -> StagedInput:
    skipped = f"; unreadable, left out: {', '.join(unreadable)}" if unreadable else ""
    if not series.latest:
        return _missing(EXAM_DIRNAME, f"no scorecards in {exam_dir}{skipped}")
    comparable = (
        "comparable"
        if series.comparable and not unreadable
        else "not comparable (case sets differ, or a scorecard is unreadable)"
    )
    return _staged(EXAM_DIRNAME, f"{len(series.latest)} case(s); previous scores {comparable}{skipped}")


class _OnceListing:
    """One read of the audited repository's open issues and PRs, shared by
    the audit, ``blocked-items.json`` and (when both name the same
    repository) ``open-issues.json``: one GitHub walk each, and every file
    describes the same moment."""

    def __init__(self, host: OpenWorkHost | Unavailable) -> None:
        self._host = host
        self._issues: Sequence[OpenIssueLabels] | None = None
        self._prs: Sequence[PRInfo] | None = None
        self._error: BaseException | None = None

    def as_host(self) -> OpenWorkHost | Unavailable:
        return self._host if isinstance(self._host, Unavailable) else _SharedHost(self)

    def as_listing(self) -> OpenIssueListing | None:
        """The shared listing, or None when the audit was told not to read GitHub."""
        return None if isinstance(self._host, Unavailable) else _SharedHost(self)

    def issues(self) -> Sequence[OpenIssueLabels]:
        if isinstance(self._host, Unavailable):
            raise RuntimeError("an unavailable host has no issues")
        if self._error is not None:
            raise self._error
        if self._issues is None:
            try:
                self._issues = self._host.list_open_issue_labels_complete()
            except Exception as error:
                # The audit records a rate limit as an unread source; the
                # open-issues read then refuses on the SAME failure rather
                # than trying GitHub a second time.
                self._error = error
                raise
        return self._issues

    def prs(self) -> Sequence[PRInfo]:
        """The open PRs, read once: the audit's read, which blocked-items.json reuses."""
        if isinstance(self._host, Unavailable):
            raise RuntimeError("an unavailable host has no pull requests")
        if self._prs is None:
            self._prs = self._host.list_open_prs_complete()
        return self._prs


class _SharedHost:
    def __init__(self, once: _OnceListing) -> None:
        self._once = once

    def list_open_issue_labels_complete(self) -> Sequence[OpenIssueLabels]:
        return self._once.issues()

    def list_open_prs_complete(self) -> Sequence[PRInfo]:
        return self._once.prs()


def load_staged_evidence(data_dir: Path) -> StagedEvidence:
    """Read a staged ``improver-data/`` back through its contracts, for the validator."""
    texts = {
        path.relative_to(data_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(data_dir.rglob("*.json"))
        if not path.relative_to(data_dir).as_posix().startswith(f"{ENGINE_SOURCE_DIRNAME}/")
    }
    documents = parse_documents(texts)
    manifest = InputsManifest.model_validate(documents[INPUTS_FILE])

    def read(name: str, model: type[M]) -> M | None:
        return None if name not in documents else model.model_validate(documents[name])

    audit = read(AUDIT_FILE, EngineAuditReport)
    start = read(ENGINE_START_FILE, EngineStartInput)
    open_issues = read(OPEN_ISSUES_FILE, OpenIssuesInput)
    if audit is None or start is None or open_issues is None:
        raise ImproverInputsUnavailable(f"{data_dir} lacks a required input")
    source = data_dir / ENGINE_SOURCE_DIRNAME
    return StagedEvidence(
        documents=documents,
        engine_id=manifest.engine_id,
        audited_repo=manifest.audited_repo,
        audit=audit,
        previous_audit=read(AUDIT_PREVIOUS_FILE, EngineAuditReport),
        diff=read(AUDIT_DIFF_FILE, AuditDiff),
        engine_start=start,
        charter=read(CHARTER_FILE, EffectiveCharter),
        decisions=read(CHARTER_DECISIONS_FILE, CharterDecisionsInput),
        case_files=read(CASE_FILES_FILE, CaseFilesInput),
        interventions=read(INTERVENTIONS_FILE, InterventionsInput),
        open_issues=open_issues,
        blocked_items=read(BLOCKED_ITEMS_FILE, BlockedItemsInput),
        existing_exam_case_ids=frozenset(manifest.existing_exam_case_ids),
        exam_comparable=manifest.exam_scores_comparable,
        engine_source_files=frozenset(
            p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()
        ),
        citations=RunDirCitations(data_dir.parent),
        engine_source=RunDirEngineSource(data_dir.parent),
    )


def _write(path: Path, model: BaseModel) -> None:
    path.write_text(to_json(model), encoding="utf-8")


def _staged(name: str, detail: str = "") -> StagedInput:
    return StagedInput(name=name, staged=True, detail=detail)


def _missing(name: str, detail: str) -> StagedInput:
    return StagedInput(name=name, staged=False, detail=detail)


__all__ = [
    "EngineSourceExporter",
    "ImproverInputStager",
    "ImproverInputsUnavailable",
    "ImproverStagingRequest",
    "OpenIssueListing",
    "StagedImproverInputs",
    "load_staged_evidence",
]
