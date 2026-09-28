"""A read-only outcome audit of one Repository Engine (#7490).

Gathers what the engine's durable stores, timeline, log and GitHub
repository say, and names the anomalies those facts show. It decides nothing
and changes nothing: every reader it is given is read-only (the stores are
opened on snapshots of the engine's databases, the log is read through
:mod:`..infra.engine_log_reader`, GitHub through two listings).

A source that cannot be read is reported, not guessed: an engine without one
of the databases gets ``absent``, a GitHub rate limit ``rate_limited`` with its
reset. Either makes the report ``partial``; neither is a crash.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from ..contracts.engine_audit import (
    ActionLivenessCount,
    ActionLivenessSection,
    Anomaly,
    AnomalyKind,
    AuditSource,
    CharterDecisionSummary,
    ClaimsSection,
    Count,
    EngineAuditReport,
    FetchCostSection,
    GitHubSection,
    LabelledIssue,
    LogCoverage,
    NoProgressSection,
    OwedPause,
    ParkedAction,
    QuarantinedClaim,
    SourceReading,
    SourceStatus,
    TechLeadSection,
    TimelineRepeat,
    UnresolvedWork,
    ValidatedWorkSection,
)
from ..control.label_manager import TECH_LEAD_NEEDS_HUMAN_LABEL
from ..control.reconciliation import RECONCILE_PAUSE_LABEL
from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL
from ..infra.engine_log_reader import EngineLogEntry, EngineLogExcerpt
from ..ports.engine_audit import (
    ActionLivenessAuditReader,
    CharterAuditReader,
    ClaimAuditReader,
    OpenWorkHost,
    PromotionAuditReader,
    TimelineAuditReader,
    TimelineEvent,
    ValidatedWorkCensusReader,
)
from ..ports.repository_host import host_rate_limit_of
from .engine_log_census import LogCensus, census_log
from .no_progress import (
    ENGINE_SUBJECT,
    LIVELOCK_THRESHOLD,
    find_current_repeats,
    subjects_changed_by,
)

#: An unresolved validated-work record older than this is stuck.
STALE_UNRESOLVED_AFTER = timedelta(hours=24)

#: Open-issue labels that ask a human (or the tech lead) to act: the prototype
#: audit's set, minus ``in-progress`` (normal work, counted but not flagged).
#: Spelled as an unprefixed engine writes them (``label_prefix`` unset, the
#: default ``needs-human``); every label is in ``label_counts`` regardless.
ATTENTION_LABELS: tuple[str, ...] = (
    "needs-human",
    TECH_LEAD_NEEDS_HUMAN_LABEL,
    "blocked-failed",
    "recovery-pending",
    RECONCILE_PAUSE_LABEL,
    PROPOSED_TECH_LEAD_LABEL,
)

#: How many charter decisions the report lists by name.
RECENT_DECISIONS = 10

#: Exhaustive open-issue listing bound; a repository with more open issues
#: fails the listing loudly rather than being counted from a partial list.
OPEN_ISSUE_SCAN_LIMIT = 2000

T = TypeVar("T")
S = TypeVar("S")


@dataclass(frozen=True)
class Unavailable:
    """A source the audit could not read, and why."""

    status: SourceStatus
    detail: str
    resets_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.status is SourceStatus.READ:
            raise ValueError("an unavailable source was not read")


@dataclass(frozen=True)
class TechLeadReaders:
    charter: CharterAuditReader
    promotions: PromotionAuditReader


@dataclass(frozen=True)
class EngineLog:
    """Reads the log once: what part of it, and its entries oldest first."""

    read: Callable[[], tuple[EngineLogExcerpt, Iterator[EngineLogEntry]]]


@dataclass(frozen=True)
class EngineAuditInputs:
    repo: str
    state_dir: Path
    validated_work: ValidatedWorkCensusReader | Unavailable
    action_liveness: ActionLivenessAuditReader | Unavailable
    tech_lead: TechLeadReaders | Unavailable
    claims: ClaimAuditReader | Unavailable
    timeline: TimelineAuditReader | Unavailable
    log: EngineLog | Unavailable
    github: OpenWorkHost | Unavailable


def audit_engine(
    inputs: EngineAuditInputs, *, now: datetime, window: timedelta
) -> EngineAuditReport:
    """One audit of the engine ``inputs`` describe, as of ``now``."""
    if now.tzinfo is None:
        raise ValueError("the audit instant must be timezone-aware")
    window_start = now - window
    readings: list[SourceReading] = []

    def read(source: AuditSource, value: T | Unavailable, section: Callable[[T], S]) -> S | None:
        return _read(readings, source, value, section)

    validated_work = read(
        AuditSource.VALIDATED_WORK, inputs.validated_work, lambda r: _validated_work(r, now)
    )
    liveness = read(AuditSource.ACTION_LIVENESS, inputs.action_liveness, _liveness)
    tech_lead = read(AuditSource.TECH_LEAD_AUTHORITY, inputs.tech_lead, _tech_lead)
    claims = read(AuditSource.PENDING_WORK_CLAIMS, inputs.claims, _claims)
    timeline_events = read(
        AuditSource.TIMELINE,
        inputs.timeline,
        lambda r: tuple(_as_engine_event(e) for e in r.events_between(window_start, now)),
    )
    events: tuple[dict[str, Any], ...] = timeline_events or ()
    log_read = read(
        AuditSource.LOG,
        inputs.log,
        lambda log: _census_log(
            log,
            inputs.repo,
            window_start,
            now,
            None if timeline_events is None else _last_state_change(events),
        ),
    )
    if log_read is not None and not log_read[1].covers(window_start):
        readings[:] = [
            SourceReading(
                source=AuditSource.LOG,
                status=SourceStatus.INCOMPLETE,
                detail="the log read does not cover the whole audit window",
            )
            if r.source is AuditSource.LOG
            else r
            for r in readings
        ]
    github = _github(inputs.github, readings)
    repeats = tuple(
        TimelineRepeat(event=r.event, subject=r.subject, detail=r.detail, count=r.count)
        for r in find_current_repeats(events)
    )
    no_progress = NoProgressSection(
        window_start=window_start.isoformat(),
        window_end=now.isoformat(),
        log=None if log_read is None else _coverage(*log_read, window_start),
        log_signatures=() if log_read is None else log_read[1].signatures,
        timeline_repeats=repeats,
    )
    fetch_cost = None if log_read is None else log_read[1].fetch_cost
    return EngineAuditReport(
        generated_at=now.isoformat(),
        repo=inputs.repo,
        state_dir=str(inputs.state_dir),
        partial=any(r.status is not SourceStatus.READ for r in readings),
        sources=tuple(readings),
        validated_work=validated_work,
        action_liveness=liveness,
        tech_lead=tech_lead,
        claims=claims,
        github=github,
        no_progress=no_progress,
        fetch_cost=fetch_cost,
        anomalies=_anomalies(
            validated_work, liveness, claims, github, no_progress, fetch_cost
        ),
    )


def _read(
    readings: list[SourceReading],
    source: AuditSource,
    value: T | Unavailable,
    section: Callable[[T], S],
) -> S | None:
    """``section`` of a readable source; None, with its reading recorded, otherwise."""
    if isinstance(value, Unavailable):
        readings.append(_unavailable(source, value))
        return None
    readings.append(SourceReading(source=source, status=SourceStatus.READ))
    return section(value)


def _unavailable(source: AuditSource, value: Unavailable) -> SourceReading:
    return SourceReading(
        source=source,
        status=value.status,
        detail=value.detail,
        resets_at=None if value.resets_at is None else value.resets_at.isoformat(),
    )


def _validated_work(reader: ValidatedWorkCensusReader, now: datetime) -> ValidatedWorkSection:
    census = reader.census()
    return ValidatedWorkSection(
        by_state_resolution=tuple(
            Count(key=(state, resolution), count=n)
            for state, resolution, n in census.by_state_resolution
        ),
        unresolved=tuple(
            UnresolvedWork(
                record_id=r.record_id,
                issue_number=r.issue_number,
                state=r.state,
                created_at=r.created_at.isoformat(),
                age_hours=round((now - r.created_at).total_seconds() / 3600, 2),
            )
            for r in census.unresolved
        ),
    )


def _liveness(reader: ActionLivenessAuditReader) -> ActionLivenessSection:
    rows = reader.all_rows()
    actions = sorted({row.key.identity.action for row in rows})
    by_action = []
    for action in actions:
        mine = [row for row in rows if row.key.identity.action == action]
        by_action.append(
            ActionLivenessCount(
                action=action,
                rows=len(mine),
                parked=sum(row.next_attempt_at is None for row in mine),
                escalated=sum(row.escalated for row in mine),
                max_attempts=max(row.attempts for row in mine),
            )
        )
    return ActionLivenessSection(
        by_action=tuple(sorted(by_action, key=lambda a: (-a.rows, a.action))),
        parked=tuple(
            ParkedAction(
                subject=row.key.identity.subject,
                action=row.key.identity.action,
                fingerprint=row.key.fingerprint,
                attempts=row.attempts,
                escalated=row.escalated,
                last_failed_at=row.last_failed_at.isoformat(),
                last_reason=row.last_reason,
            )
            for row in rows
            if row.next_attempt_at is None
        ),
        owed_pauses=tuple(
            OwedPause(issue_number=p.issue_number, reason=p.reason, attempts=p.debt.attempts)
            for p in reader.pending_pauses()
        ),
    )


def _tech_lead(readers: TechLeadReaders) -> TechLeadSection:
    promotions = Counter(p.state for p in readers.promotions.list_promotions())
    return TechLeadSection(
        charter_by_role_outcome=tuple(
            Count(key=(role, outcome), count=n)
            for role, outcome, n in readers.charter.role_outcome_counts()
        ),
        recent_decisions=tuple(
            CharterDecisionSummary(
                decided_at=d.decided_at,
                role=d.role.value,
                action_kind=d.action_kind,
                target_number=d.target_number,
                outcome=d.outcome.value,
                took_effect=d.took_effect,
            )
            for d in readers.charter.list_recent(limit=RECENT_DECISIONS)
        ),
        promotions_by_state=tuple(
            Count(key=(str(state),), count=n)
            for state, n in sorted(promotions.items(), key=lambda kv: (-kv[1], str(kv[0])))
        ),
    )


def _claims(reader: ClaimAuditReader) -> ClaimsSection:
    unresolved = reader.list_unresolved_claims()
    return ClaimsSection(
        held=sum(not c.deferred for c in unresolved),
        deferred=sum(c.deferred for c in unresolved),
        unreadable_issues=tuple(sorted(c.issue_number for c in reader.list_unreadable_claims())),
        quarantined=tuple(
            QuarantinedClaim(
                quarantine_key=q.quarantine_key,
                issue_number=q.issue_number,
                cause="" if q.cause is None else str(q.cause.value),
                releasing=q.releasing,
            )
            for q in sorted(reader.list_quarantines(), key=lambda q: q.quarantine_key)
        ),
    )


def _github(
    host: OpenWorkHost | Unavailable, readings: list[SourceReading]
) -> GitHubSection | None:
    if isinstance(host, Unavailable):
        readings.append(_unavailable(AuditSource.GITHUB, host))
        return None
    try:
        issues = host.list_issues(state="open", limit=OPEN_ISSUE_SCAN_LIMIT, exhaustive=True)
        prs = host.list_open_prs_complete()
    except Exception as error:
        limit = host_rate_limit_of(error)
        if limit is None:
            raise
        readings.append(
            _unavailable(
                AuditSource.GITHUB,
                Unavailable(SourceStatus.RATE_LIMITED, str(error), limit.resets_at),
            )
        )
        return None
    readings.append(SourceReading(source=AuditSource.GITHUB, status=SourceStatus.READ))
    labels = Counter(label for issue in issues for label in set(issue.labels))
    drafts = []
    ready = 0
    for pr in prs:
        if pr.draft is None:
            raise ValueError(f"open PR #{pr.number} did not report whether it is a draft")
        if pr.draft:
            drafts.append(pr.number)
        else:
            ready += 1
    return GitHubSection(
        open_issues=len(issues),
        label_counts=tuple(
            Count(key=(label,), count=n)
            for label, n in sorted(labels.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        attention=tuple(
            LabelledIssue(label=label, issue_number=issue.number)
            for label in ATTENTION_LABELS
            for issue in sorted(issues, key=lambda i: i.number)
            if label in issue.labels
        ),
        open_prs_ready=ready,
        draft_prs=tuple(sorted(drafts)),
    )


def _as_engine_event(event: TimelineEvent) -> dict[str, Any]:
    """A timeline row in the event shape :mod:`.no_progress` reads."""
    record = event.record
    return {
        "type": record.source_event or record.event,
        "issue_key": str(event.issue_number),
        "payload": record.data,
        "timestamp": record.timestamp,
    }


def _last_state_change(events: Iterable[dict[str, Any]]) -> dict[str, datetime]:
    changed: dict[str, datetime] = {}
    for event in events:
        for subject in subjects_changed_by(event):
            changed[subject] = datetime.fromisoformat(event["timestamp"])
    return changed


def _census_log(
    log: EngineLog,
    repo: str,
    window_start: datetime,
    window_end: datetime,
    last_state_change: dict[str, datetime] | None,
) -> tuple[EngineLogExcerpt, LogCensus]:
    excerpt, entries = log.read()
    return excerpt, census_log(
        entries,
        window_start=window_start,
        window_end=window_end,
        last_state_change=last_state_change,
        repo=repo,
    )


def _coverage(
    excerpt: EngineLogExcerpt, census: LogCensus, window_start: datetime
) -> LogCoverage:
    return LogCoverage(
        path=str(excerpt.path),
        bytes_read=excerpt.bytes_read,
        truncated=excerpt.truncated,
        first_read_at=None if census.first_read_at is None else census.first_read_at.isoformat(),
        covers_window=census.covers(window_start),
        first_entry_at=None if census.first_entry_at is None else census.first_entry_at.isoformat(),
        last_entry_at=None if census.last_entry_at is None else census.last_entry_at.isoformat(),
    )


def _anomalies(
    validated_work: ValidatedWorkSection | None,
    liveness: ActionLivenessSection | None,
    claims: ClaimsSection | None,
    github: GitHubSection | None,
    no_progress: NoProgressSection,
    fetch_cost: FetchCostSection | None,
) -> tuple[Anomaly, ...]:
    found: list[Anomaly] = []
    if validated_work is not None:
        stale_hours = STALE_UNRESOLVED_AFTER.total_seconds() / 3600
        found.extend(
            Anomaly(
                kind=AnomalyKind.STALE_UNRESOLVED_WORK,
                sources=(AuditSource.VALIDATED_WORK,),
                subject=f"#{w.issue_number}",
                signature=w.record_id,
                detail=f"{w.state} for {w.age_hours:.1f}h",
            )
            for w in validated_work.unresolved
            if w.age_hours >= stale_hours
        )
    if liveness is not None:
        found.extend(
            Anomaly(
                kind=AnomalyKind.PARKED_ACTION,
                sources=(AuditSource.ACTION_LIVENESS,),
                subject=p.subject,
                signature=f"{p.action}:{p.fingerprint}",
                detail=("escalated; " if p.escalated else "") + p.last_reason,
                count=p.attempts,
            )
            for p in liveness.parked
        )
        found.extend(
            Anomaly(
                kind=AnomalyKind.OWED_PAUSE,
                sources=(AuditSource.ACTION_LIVENESS,),
                subject=f"#{p.issue_number}",
                signature="reconcile_pause",
                detail=p.reason,
                count=p.attempts,
            )
            for p in liveness.owed_pauses
        )
    if claims is not None:
        found.extend(
            Anomaly(
                kind=AnomalyKind.UNREADABLE_CLAIM,
                sources=(AuditSource.PENDING_WORK_CLAIMS,),
                subject=f"#{n}",
                signature="unreadable_claim",
                detail="stored claim payload cannot be read back",
            )
            for n in claims.unreadable_issues
        )
        found.extend(
            Anomaly(
                kind=AnomalyKind.QUARANTINED_CLAIM,
                sources=(AuditSource.PENDING_WORK_CLAIMS,),
                subject=f"#{q.issue_number}",
                signature=q.quarantine_key,
                detail=q.cause + ("; releasing" if q.releasing else ""),
            )
            for q in claims.quarantined
        )
    if github is not None:
        found.extend(
            Anomaly(
                kind=AnomalyKind.ATTENTION_LABEL,
                sources=(AuditSource.GITHUB,),
                subject=f"#{a.issue_number}",
                signature=a.label,
                detail=f"open issue carries {a.label}",
            )
            for a in github.attention
        )
        found.extend(
            Anomaly(
                kind=AnomalyKind.DRAFT_PR,
                sources=(AuditSource.GITHUB,),
                subject=f"PR #{n}",
                signature="draft",
                detail="open pull request is a draft",
            )
            for n in github.draft_prs
        )
    found.extend(
        Anomaly(
            kind=AnomalyKind.NO_PROGRESS_LOG,
            sources=(AuditSource.LOG, AuditSource.TIMELINE),
            subject=s.subject,
            signature=f"{s.level} {s.logger}: {s.signature}",
            detail=f"{s.since_state_change} since the subject last changed state"
            f" ({s.count} in the window)",
            count=s.since_state_change,
        )
        for s in no_progress.log_signatures
        if s.since_state_change is not None and s.since_state_change >= LIVELOCK_THRESHOLD
    )
    found.extend(
        Anomaly(
            kind=AnomalyKind.NO_PROGRESS_TIMELINE,
            sources=(AuditSource.TIMELINE,),
            subject=r.subject,
            signature=f"{r.event} [{r.detail}]",
            detail=f"repeated {r.count}x with no state change",
            count=r.count,
        )
        for r in no_progress.timeline_repeats
    )
    if fetch_cost is not None:
        found.extend(_fetch_cost_anomalies(fetch_cost))
    return tuple(found)


def _fetch_cost_anomalies(cost: FetchCostSection) -> Iterator[Anomaly]:
    modes = {m.mode: m for m in cost.by_mode}
    full, incremental = modes.get("full"), modes.get("incremental")
    if full is not None and incremental is not None and (
        incremental.gh_calls_median > full.gh_calls_median
    ):
        yield Anomaly(
            kind=AnomalyKind.FETCH_COST_INVERTED,
            sources=(AuditSource.LOG,),
            subject=ENGINE_SUBJECT,
            signature="incremental_over_full",
            detail=f"incremental refresh median {incremental.gh_calls_median:g} GitHub calls"
            f" vs full {full.gh_calls_median:g}",
        )
    # Most iterations, not one: a single repeat can be a refresh racing a
    # launch; a majority is how the engine reads.
    if cost.cycles and cost.cycles_with_repeat_reads * 2 > cost.cycles:
        worst = cost.worst_cycle
        detail = (
            f"{cost.cycles_with_repeat_reads}/{cost.cycles} iterations re-read an issue"
            + (
                ""
                if worst is None
                else f"; worst {worst.single_issue_gets} GETs for {worst.distinct_issues} issues"
            )
        )
        yield Anomaly(
            kind=AnomalyKind.REPEATED_ISSUE_READS,
            sources=(AuditSource.LOG,),
            subject=ENGINE_SUBJECT,
            signature="repeat_issue_reads",
            detail=detail,
            count=cost.cycles_with_repeat_reads,
        )


__all__ = [
    "ATTENTION_LABELS",
    "EngineAuditInputs",
    "EngineLog",
    "STALE_UNRESOLVED_AFTER",
    "TechLeadReaders",
    "Unavailable",
    "audit_engine",
]
