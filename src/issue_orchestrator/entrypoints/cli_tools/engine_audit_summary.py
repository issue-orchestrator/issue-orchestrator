"""The human summary of an engine audit report (#7490).

A reading of the JSON report, never a second source of facts: everything it
prints is a field of :class:`~...contracts.engine_audit.EngineAuditReport`.
"""

from __future__ import annotations

from ...contracts.engine_audit import (
    Anomaly,
    AuditDiff,
    Count,
    EngineAuditReport,
    SourceStatus,
)
from ...observation.engine_audit import ATTENTION_LABELS, STALE_UNRESOLVED_AFTER

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
    if (vw := report.validated_work) is not None:
        lines.append("== validated work: state x resolution")
        lines.extend(_counts(vw.by_state_resolution))
        stale_hours = STALE_UNRESOLVED_AFTER.total_seconds() / 3600
        stale = [w for w in vw.unresolved if w.age_hours >= stale_hours]
        lines.append(
            f"== validated work: {len(vw.unresolved)} unresolved,"
            f" {len(stale)} older than {stale_hours:g}h"
        )
    if (lv := report.action_liveness) is not None:
        lines.append("== liveness: action | rows | parked | escalated | max attempts")
        lines.extend(
            f"  {a.action} | {a.rows} | {a.parked} | {a.escalated} | {a.max_attempts}"
            for a in lv.by_action
        )
        lines.append(f"== liveness: owed reconcile pauses: {len(lv.owed_pauses)}")
    if (tl := report.tech_lead) is not None:
        lines.append("== charter decisions (role, outcome)")
        lines.extend(_counts(tl.charter_by_role_outcome))
        lines.append("== charter decisions: most recent")
        lines.extend(
            f"  {d.decided_at} | {d.role} | {d.action_kind} | {d.target_number} | {d.outcome}"
            f" | took_effect={d.took_effect}"
            for d in tl.recent_decisions
        )
        lines.append("== promoted findings by state")
        lines.extend(_counts(tl.promotions_by_state))
    if (cl := report.claims) is not None:
        lines.append(
            f"== claims: held {cl.held}, deferred {cl.deferred},"
            f" unreadable {len(cl.unreadable_issues)}, quarantined {len(cl.quarantined)}"
        )
    if (gh := report.github) is not None:
        lines.append(f"== GitHub: {gh.open_issues} open issues")
        counts = {c.key[0]: c.count for c in gh.label_counts}
        lines.extend(
            f"  {label}: {counts.get(label, 0)}" for label in (*ATTENTION_LABELS, "in-progress")
        )
        lines.append(f"== open PRs: ready {gh.open_prs_ready}, draft {len(gh.draft_prs)}")
    if (fc := report.fetch_cost) is not None:
        lines.append("== GitHub fetch cost (log): mode | refreshes | calls median/max | ms median/max")
        lines.extend(
            f"  {m.mode} | {m.refreshes} | {m.gh_calls_median:g}/{m.gh_calls_max}"
            f" | {m.duration_ms_median:g}/{m.duration_ms_max}"
            for m in fc.by_mode
        )
        per_hour = "-" if fc.refresh_calls_per_hour is None else f"{fc.refresh_calls_per_hour:.0f}"
        lines.append(
            f"  refresh calls/h {per_hour}; {fc.cycles_with_repeat_reads}/{fc.cycles}"
            " iterations re-read an issue"
            + (
                ""
                if fc.worst_cycle is None
                else f" (worst: {fc.worst_cycle.single_issue_gets} GETs for"
                f" {fc.worst_cycle.distinct_issues} issues)"
            )
        )
    np = report.no_progress
    lines.append(f"== no progress since {np.window_start}")
    lines.extend(
        f"  {s.since_state_change}x ({s.count} in window) {s.subject} {s.level} {s.signature}"
        for s in sorted(np.log_signatures, key=lambda s: -s.since_state_change)[:TOP]
    )
    lines.extend(f"  timeline: {r.count}x {r.subject} {r.event} [{r.detail}]" for r in np.timeline_repeats)
    lines.append(f"== anomalies: {len(report.anomalies)}")
    lines.extend(_anomalies(report.anomalies))
    if report.diff is not None:
        lines.extend(_diff(report.diff))
    return "\n".join(lines)


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
