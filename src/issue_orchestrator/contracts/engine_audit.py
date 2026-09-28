"""The engine audit report: the machine contract ``io engine-audit`` emits (#7490).

One read-only outcome audit of a Repository Engine: what its durable state,
its log, its timeline and its GitHub repository say, plus the anomalies those
facts show and, when a previous report is given, how the anomalies moved. The
tech lead's health review and the second-order improver both consume it, so
its shape is versioned: :data:`ENGINE_AUDIT_SCHEMA_VERSION` changes whenever a
field changes meaning, and a report of another version is refused rather than
compared field by field.

Every model is frozen and forbids unknown fields; a report read back from
disk is validated, not trusted.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: Bump when a field is added, removed or changes meaning.
ENGINE_AUDIT_SCHEMA_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuditSource(StrEnum):
    """Where a section's facts come from."""

    VALIDATED_WORK = "validated_work"
    ACTION_LIVENESS = "action_liveness"
    TECH_LEAD_AUTHORITY = "tech_lead_authority"
    PENDING_WORK_CLAIMS = "pending_work_claims"
    TIMELINE = "timeline"
    LOG = "log"
    GITHUB = "github"


class SourceStatus(StrEnum):
    """Whether a source was read, and if not, why."""

    READ = "read"
    #: The engine has no such database (an older engine, or a fresh one).
    ABSENT = "absent"
    #: The host refused on a rate limit; the section is missing until it resets.
    RATE_LIMITED = "rate_limited"
    #: The operator asked for it not to be read (``--no-github``).
    SKIPPED = "skipped"


class SourceReading(_Frozen):
    source: AuditSource
    status: SourceStatus
    detail: str = ""
    #: When a rate-limited source can be read again.
    resets_at: str | None = None


class Count(_Frozen):
    """One cell of a grouped count, e.g. ``["abandoned", "outside_recovery_scope"]``."""

    key: tuple[str, ...]
    count: int = Field(ge=0)


class UnresolvedWork(_Frozen):
    record_id: str
    issue_number: int
    state: str
    created_at: str
    age_hours: float


class ValidatedWorkSection(_Frozen):
    #: ``[state, resolution_kind]``; an unresolved record's resolution is ``""``.
    by_state_resolution: tuple[Count, ...]
    unresolved: tuple[UnresolvedWork, ...]


class ActionLivenessCount(_Frozen):
    action: str
    rows: int
    parked: int
    escalated: int
    max_attempts: int


class ParkedAction(_Frozen):
    subject: str
    action: str
    fingerprint: str
    attempts: int
    escalated: bool
    last_failed_at: str
    last_reason: str


class OwedPause(_Frozen):
    issue_number: int
    reason: str
    attempts: int


class ActionLivenessSection(_Frozen):
    by_action: tuple[ActionLivenessCount, ...]
    parked: tuple[ParkedAction, ...]
    owed_pauses: tuple[OwedPause, ...]


class CharterDecisionSummary(_Frozen):
    decided_at: str
    role: str
    action_kind: str
    target_number: int | None
    outcome: str
    took_effect: bool


class TechLeadSection(_Frozen):
    #: ``[role, outcome]``.
    charter_by_role_outcome: tuple[Count, ...]
    recent_decisions: tuple[CharterDecisionSummary, ...]
    #: ``[state]``.
    promotions_by_state: tuple[Count, ...]


class QuarantinedClaim(_Frozen):
    quarantine_key: str
    issue_number: int
    cause: str
    releasing: bool


class ClaimsSection(_Frozen):
    held: int
    deferred: int
    #: Issue numbers of stored claims whose payload cannot be read back.
    unreadable_issues: tuple[int, ...]
    quarantined: tuple[QuarantinedClaim, ...]


class LabelledIssue(_Frozen):
    label: str
    issue_number: int


class GitHubSection(_Frozen):
    open_issues: int
    #: Every label on an open issue, ``[label]``.
    label_counts: tuple[Count, ...]
    #: Open issues carrying a label that asks for attention.
    attention: tuple[LabelledIssue, ...]
    open_prs_ready: int
    draft_prs: tuple[int, ...]


class LogSignature(_Frozen):
    """One normalized ERROR/WARNING shape the engine logged about one subject."""

    subject: str
    level: str
    logger: str
    signature: str
    #: Occurrences inside the audit window.
    count: int
    #: Occurrences since the subject last changed state (its timeline); every
    #: occurrence for a subject with no state change in the window.
    since_state_change: int
    first_seen: str
    last_seen: str


class FetchModeCost(_Frozen):
    """The engine's queue refreshes of one mode (``[FETCH-COST]`` log lines)."""

    mode: str
    refreshes: int
    gh_calls_total: int
    gh_calls_median: float
    gh_calls_max: int
    gh_errors_total: int
    duration_ms_median: float
    duration_ms_max: int


class IssueReadCycle(_Frozen):
    """Single-issue GETs one engine loop iteration made (httpx request lines)."""

    started_at: str
    single_issue_gets: int
    distinct_issues: int

    @property
    def repeat_reads(self) -> int:
        return self.single_issue_gets - self.distinct_issues


class FetchCostSection(_Frozen):
    by_mode: tuple[FetchModeCost, ...]
    #: Refresh calls per hour between the first and last refresh in the
    #: window; None with fewer than two refreshes.
    refresh_calls_per_hour: float | None
    #: Loop iterations (``[LOOP] Iteration`` lines) seen, and how many of them
    #: read the same issue more than once.
    cycles: int
    cycles_with_repeat_reads: int
    #: The iteration with the most repeated single-issue reads.
    worst_cycle: IssueReadCycle | None
    #: Single-issue GET lines seen at all. Zero means the engine does not log
    #: its requests (httpx pinned to WARNING), not that it made none.
    issue_get_lines: int


class LogCoverage(_Frozen):
    path: str
    bytes_read: int
    #: True when only the log's tail was read.
    truncated: bool
    #: The first and last entries read inside the audit window. A first entry
    #: well after ``window_start`` means the bounded tail did not reach back
    #: to it, so the log counts cover less than the window.
    first_entry_at: str | None
    last_entry_at: str | None


class TimelineRepeat(_Frozen):
    """A failure event repeated for one subject with no state change in between."""

    event: str
    subject: str
    detail: str
    count: int


class NoProgressSection(_Frozen):
    window_start: str
    window_end: str
    #: None when the log was not read (see ``sources``).
    log: LogCoverage | None
    log_signatures: tuple[LogSignature, ...]
    timeline_repeats: tuple[TimelineRepeat, ...]


class AnomalyKind(StrEnum):
    STALE_UNRESOLVED_WORK = "stale_unresolved_work"
    PARKED_ACTION = "parked_action"
    OWED_PAUSE = "owed_pause"
    UNREADABLE_CLAIM = "unreadable_claim"
    QUARANTINED_CLAIM = "quarantined_claim"
    ATTENTION_LABEL = "attention_label"
    DRAFT_PR = "draft_pr"
    NO_PROGRESS_LOG = "no_progress_log"
    NO_PROGRESS_TIMELINE = "no_progress_timeline"
    #: Incremental refreshes cost more GitHub calls than full ones.
    FETCH_COST_INVERTED = "fetch_cost_inverted"
    #: Most loop iterations read the same issue more than once.
    REPEATED_ISSUE_READS = "repeated_issue_reads"


class Anomaly(_Frozen):
    """One thing the audit found wrong, keyed so two audits can be compared.

    ``(kind, subject, signature)`` identifies it across runs; ``detail`` and
    ``count`` describe this run's observation of it and may change.
    """

    kind: AnomalyKind
    source: AuditSource
    subject: str
    signature: str
    detail: str
    count: int | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind.value, self.subject, self.signature)


class PersistingAnomaly(_Frozen):
    anomaly: Anomaly
    previous_count: int | None


class AuditDiff(_Frozen):
    previous_generated_at: str
    new: tuple[Anomaly, ...]
    resolved: tuple[Anomaly, ...]
    persisting: tuple[PersistingAnomaly, ...]
    #: Previous anomalies whose source this audit could not read: neither
    #: resolved nor persisting, because nothing was observed about them.
    unobserved: tuple[Anomaly, ...]


class EngineAuditReport(_Frozen):
    schema_version: Literal[1] = ENGINE_AUDIT_SCHEMA_VERSION
    generated_at: str
    repo: str
    state_dir: str
    #: True when any source could not be read (see ``sources``).
    partial: bool
    sources: tuple[SourceReading, ...]
    validated_work: ValidatedWorkSection | None
    action_liveness: ActionLivenessSection | None
    tech_lead: TechLeadSection | None
    claims: ClaimsSection | None
    github: GitHubSection | None
    no_progress: NoProgressSection
    #: None when the log was not read.
    fetch_cost: FetchCostSection | None
    anomalies: tuple[Anomaly, ...]
    diff: AuditDiff | None = None

    def unread_sources(self) -> frozenset[AuditSource]:
        return frozenset(r.source for r in self.sources if r.status is not SourceStatus.READ)


__all__ = [
    "ENGINE_AUDIT_SCHEMA_VERSION",
    "ActionLivenessCount",
    "ActionLivenessSection",
    "Anomaly",
    "AnomalyKind",
    "AuditDiff",
    "AuditSource",
    "CharterDecisionSummary",
    "ClaimsSection",
    "Count",
    "FetchCostSection",
    "FetchModeCost",
    "IssueReadCycle",
    "LogCoverage",
    "EngineAuditReport",
    "GitHubSection",
    "LabelledIssue",
    "LogSignature",
    "NoProgressSection",
    "OwedPause",
    "ParkedAction",
    "PersistingAnomaly",
    "QuarantinedClaim",
    "SourceReading",
    "SourceStatus",
    "TechLeadSection",
    "TimelineRepeat",
    "UnresolvedWork",
    "ValidatedWorkSection",
]
