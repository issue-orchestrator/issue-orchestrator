"""Durable needs-human label-clear reconciliation for recovered triage launches.

When a queued failure investigation exhausts its bounded launch retries, the
orchestrator escalates it to needs-human by applying the source-of-truth label
first, then an explanatory comment. If the label lands but the comment fails,
``PendingTriageReview.needs_human_escalation_incomplete`` records that GitHub now
carries a stale needs-human marker. If a later tick's launch prep recovers and
the investigation launches, that marker contradicts the running work and must be
cleared.

Clearing is a real external mutation and can itself fail. Rather than destroy the
reconciliation record on a failed clear (the round-5 gap closed here in round 6),
the launch path records the issue number in
``OrchestratorState.pending_needs_human_label_clears`` and a per-tick reconciler
retries the label removal — and only the removal — until it commits. It never
launches or relaunches anything: the investigation is already running.
"""

from typing import TYPE_CHECKING

from ..domain.models import PendingTriageReview
from .session_launcher import SessionLauncher

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from .label_manager import LabelManager


def clear_stale_needs_human_on_launch(
    triage: PendingTriageReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
) -> None:
    """Clear a needs-human label an incomplete escalation left behind (#6771 r5/r6).

    A prior tick may have exhausted launch retries and applied the needs-human
    source-of-truth label but failed to commit the escalation (comment failed).
    If prep then recovers and the investigation launches, that label is stale and
    contradicts the running work, so the successful-launch path clears it through
    the launcher's owning action boundary.

    The queued item is removed by the caller regardless (the session is running;
    it must not be relaunched), so the per-item flag cannot outlive it. If the
    removal action does NOT commit, the issue number is transferred to the durable
    ``pending_needs_human_label_clears`` reconciliation list so a later tick's
    reconciler retries the removal instead of silently leaving stale state on
    GitHub (#6771 round 6)."""
    if not triage.needs_human_escalation_incomplete:
        return
    triage.needs_human_escalation_incomplete = False
    removal_committed = session_launcher.clear_needs_human_label(triage.issue_number)
    if (
        not removal_committed
        and triage.issue_number not in state.pending_needs_human_label_clears
    ):
        state.pending_needs_human_label_clears.append(triage.issue_number)


def reconcile_pending_needs_human_label_clears(
    state: "OrchestratorState", session_launcher: SessionLauncher
) -> None:
    """Retry stale needs-human label removals recorded by recovered launches.

    Per-tick drain of ``pending_needs_human_label_clears``: for each recorded
    issue number, re-attempt the label removal through the launcher's owning
    action boundary. On commit, drop the issue from the list. This retries the
    external label removal ONLY — it never launches or relaunches a session (the
    investigation that superseded the stale marker is already running)."""
    for issue_number in list(state.pending_needs_human_label_clears):
        removal_committed = session_launcher.clear_needs_human_label(issue_number)
        if removal_committed:
            state.pending_needs_human_label_clears.remove(issue_number)


def reconstruct_pending_needs_human_label_clears(
    state: "OrchestratorState", label_manager: "LabelManager"
) -> list[int]:
    """Rebuild the needs-human clear list lost across a process restart (#6771 r7).

    ``OrchestratorState.pending_needs_human_label_clears`` is memory-only: if the
    process exits after a stale-clear ``RemoveLabelAction`` fails but before a
    reconcile tick commits it, the record is dropped while GitHub still carries
    the needs-human source-of-truth marker — leaving a running investigation
    permanently contradictory (the exact invariant this owner closes). Rather
    than add a second persistent store, startup rebuilds the list from durable
    facts (labels-as-truth, crash-safe from labels — ADR-0013).

    Reconstruction rule: a STALE needs-human marker is one whose issue has an
    active/restored session (the investigation that superseded the escalation is
    running) yet still carries the needs-human label; it is seeded so the
    per-tick reconciler retries the removal — and only the removal. A LEGITIMATE
    committed needs-human escalation is for an issue whose investigation could
    NOT launch (retries exhausted): it has no active/restored session, so it is
    never seeded and its label is preserved. Idempotent: an issue already queued
    for a clear (or seen twice among active sessions) is not re-added.
    """
    already = set(state.pending_needs_human_label_clears)
    seeded: list[int] = []
    for session in state.active_sessions:
        issue_number = session.issue.number
        if issue_number in already or not label_manager.requires_human_any(session.issue.labels):
            continue
        already.add(issue_number)
        seeded.append(issue_number)
    state.pending_needs_human_label_clears.extend(seeded)
    return seeded
