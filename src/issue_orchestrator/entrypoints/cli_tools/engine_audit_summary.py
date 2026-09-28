"""The human summary of an engine audit report (#7490).

A reading of the JSON report, never a second source of facts: everything it
prints is a field of :class:`~...contracts.engine_audit.EngineAuditReport`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from ...contracts.engine_audit import (
    ActionLivenessSection,
    Anomaly,
    AuditDiff,
    ClaimsSection,
    Count,
    EngineAuditReport,
    FetchCostSection,
    GitHubSection,
    NoProgressSection,
    SourceStatus,
    TechLeadSection,
    ValidatedWorkSection,
)
from ...observation.engine_audit import ATTENTION_LABELS, STALE_UNRESOLVED_AFTER

S = TypeVar("S")

#: How many rows of a long list the summary shows.
TOP = 10


def render_summary(report: EngineAuditReport) -> str:
    lines = [
        f"######## ENGINE AUDIT {report.repo}  {report.generated_at}"
        + ("  [PARTIAL]" if report.partial else ""),
    ]
    for reading in report.sources:
        if reading.status is not SourceStatus.READ:
            resets = f" until {reading.resets_at}" if reading.resets_at else ""
            lines.append(f"  !! {reading.source.value}: {reading.status.value}{resets} {reading.detail}")
    lines.extend(_section(report.validated_work, _validated_work))
    lines.extend(_section(report.action_liveness, _liveness))
    lines.extend(_section(report.tech_lead, _tech_lead))
    lines.extend(_section(report.claims, _claims))
    lines.extend(_section(report.github, _github))
    lines.extend(_section(report.fetch_cost, _fetch_cost))
    lines.extend(_no_progress(report.no_progress))
    lines.append(f"== anomalies: {len(report.anomalies)}")
    lines.extend(_anomalies(report.anomalies))
    if report.diff is not None:
        lines.extend(_diff(report.diff))
    return "\n".join(lines)


def _section(section: S | None, render: Callable[[S], list[str]]) -> list[str]:
    """A section's lines; none for a section whose source was not read."""
    return [] if section is None else render(section)


def _validated_work(vw: ValidatedWorkSection) -> list[str]:
    stale_hours = STALE_UNRESOLVED_AFTER.total_seconds() / 3600
    stale = [w for w in vw.unresolved if w.age_hours >= stale_hours]
    return [
        "== validated work: state x resolution",
        *_counts(vw.by_state_resolution),
        f"== validated work: {len(vw.unresolved)} unresolved,"
        f" {len(stale)} older than {stale_hours:g}h",
    ]


def _liveness(lv: ActionLivenessSection) -> list[str]:
    return [
        "== liveness: action | rows | parked | escalated | max attempts",
        *(
            f"  {a.action} | {a.rows} | {a.parked} | {a.escalated} | {a.max_attempts}"
            for a in lv.by_action
        ),
        f"== liveness: owed reconcile pauses: {len(lv.owed_pauses)}",
    ]


def _tech_lead(tl: TechLeadSection) -> list[str]:
    return [
        "== charter decisions: role | action | outcome | effect",
        *_counts(tl.charter_effects),
        "== charter decisions: most recent",
        *(
            f"  {d.decided_at} | {d.role} | {d.action_kind} | {d.target_number} | {d.outcome}"
            f" | {d.effect}" + (f" ({d.execution_reason[:80]})" if d.execution_reason else "")
            for d in tl.recent_decisions
        ),
        "== promoted findings by state",
        *_counts(tl.promotions_by_state),
    ]


def _claims(cl: ClaimsSection) -> list[str]:
    return [
        f"== claims: held {cl.held}, deferred {cl.deferred},"
        f" unreadable {len(cl.unreadable)}, quarantined {len(cl.quarantined)}"
    ]


def _github(gh: GitHubSection) -> list[str]:
    counts = {c.key[0]: c.count for c in gh.label_counts}
    return [
        f"== GitHub: {gh.open_issues} open issues",
        *(f"  {label}: {counts.get(label, 0)}" for label in (*ATTENTION_LABELS, "in-progress")),
        f"== open PRs: ready {gh.open_prs_ready}, draft {len(gh.draft_prs)}",
    ]


def _fetch_cost(fc: FetchCostSection) -> list[str]:
    lines = ["== GitHub fetch cost (log): mode | refreshes | calls median/max | ms median/max"]
    lines.extend(
        f"  {m.mode} | {m.refreshes} | {m.gh_calls_median:g}/{m.gh_calls_max}"
        f" | {m.duration_ms_median:g}/{m.duration_ms_max}"
        for m in fc.by_mode
    )
    if fc.issue_get_lines == 0:
        lines.append("  repeat reads unmeasured: the log has no request lines (httpx below INFO)")
    per_hour = "-" if fc.refresh_calls_per_hour is None else f"{fc.refresh_calls_per_hour:.0f}"
    worst = (
        ""
        if fc.worst_cycle is None
        else f" (worst: {fc.worst_cycle.single_issue_gets} GETs for"
        f" {fc.worst_cycle.distinct_issues} issues)"
    )
    lines.append(
        f"  refresh calls/h {per_hour}; {fc.cycles_with_repeat_reads}/{fc.cycles}"
        f" iterations re-read an issue{worst}"
    )
    return lines


def _no_progress(np: NoProgressSection) -> list[str]:
    return [
        f"== no progress since {np.window_start}",
        *(
            f"  {'?' if s.since_state_change is None else s.since_state_change}x"
            f" ({s.count} in window) {s.subject} {s.level} {s.signature}"
            for s in sorted(np.log_signatures, key=lambda s: -(s.since_state_change or s.count))[:TOP]
        ),
        *(f"  timeline: {r.count}x {r.subject} {r.event} [{r.detail}]" for r in np.timeline_repeats),
    ]


def _counts(counts: tuple[Count, ...]) -> list[str]:
    return [f"  {' | '.join(k or '-' for k in c.key)} | {c.count}" for c in counts]


def _anomalies(anomalies: tuple[Anomaly, ...]) -> list[str]:
    by_kind: dict[str, int] = {}
    for anomaly in anomalies:
        by_kind[anomaly.kind.value] = by_kind.get(anomaly.kind.value, 0) + 1
    return [f"  {kind}: {n}" for kind, n in sorted(by_kind.items())]


def _diff(diff: AuditDiff) -> list[str]:
    lines = [
        f"== since {diff.previous_generated_at}: {len(diff.new)} new, {len(diff.resolved)} resolved,"
        f" {len(diff.persisting)} persisting, {len(diff.unobserved)} unobserved"
    ]
    lines.extend(f"  + {a.kind.value} {a.subject} {a.signature[:100]}" for a in diff.new[:TOP])
    lines.extend(f"  - {a.kind.value} {a.subject} {a.signature[:100]}" for a in diff.resolved[:TOP])
    return lines


__all__ = ["render_summary"]
