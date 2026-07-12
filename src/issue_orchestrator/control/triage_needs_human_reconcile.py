"""Durable needs-human label-clear reconciliation for recovered triage launches.

When a queued failure investigation exhausts its bounded launch retries, the
orchestrator escalates it to needs-human by applying the source-of-truth label
first, then an explanatory comment. If the label lands but the comment fails,
``PendingTriageReview.needs_human_escalation_incomplete`` records that GitHub now
carries a stale needs-human marker. If a later tick's launch prep recovers and
the investigation launches, that marker contradicts the running work and must be
cleared.

Clearing is a real external mutation and can itself fail. Rather than destroy the
reconciliation record on a failed clear (the round-5 gap closed in round 6), the
launch path persists the issue number to the launcher-owned
``NeedsHumanClearStore`` and a per-tick reconciler retries the label removal —
and only the removal — until it commits. It never launches or relaunches
anything: the investigation is already running.

That store is durable, so it is also the crash-safe provenance the round-7
finding requires: a restart reconciles exactly the incomplete removals the
orchestrator itself initiated. It never infers ownership from "an active session
still carries needs-human" — a conjunction an operator's stop or a running
session's own needs-human transition also satisfies, which would strip a
legitimate escalation on the next tick (#6771 round 7).
"""

from ..domain.models import PendingTriageReview
from .session_launcher import SessionLauncher


def clear_stale_needs_human_on_launch(
    triage: PendingTriageReview,
    session_launcher: SessionLauncher,
) -> None:
    """Clear a needs-human label an incomplete escalation left behind (#6771 r5-r7).

    A prior tick may have exhausted launch retries and applied the needs-human
    source-of-truth label but failed to commit the escalation (comment failed).
    If prep then recovers and the investigation launches, that label is stale and
    contradicts the running work, so the successful-launch path clears it through
    the launcher's owning action boundary.

    The queued item is removed by the caller regardless (the session is running;
    it must not be relaunched), so the per-item flag cannot outlive it. If the
    removal action does NOT commit, the issue number is persisted to the durable
    ``NeedsHumanClearStore`` so a later tick's reconciler retries the removal
    instead of leaving stale state on GitHub, and the record survives a restart
    as proof the orchestrator owns this clear (#6771 round 6/7)."""
    if not triage.needs_human_escalation_incomplete:
        return
    triage.needs_human_escalation_incomplete = False
    _reconcile_stale_needs_human_clear(triage.issue_number, session_launcher)


def reconcile_pending_needs_human_label_clears(
    session_launcher: SessionLauncher,
) -> None:
    """Retry stale needs-human label removals the launcher durably owns.

    Per-tick drain of the launcher-owned ``NeedsHumanClearStore``: for each
    recorded issue number, re-attempt the label removal through the launcher's
    owning action boundary and drop the record on commit. Because the store is
    durable, this same drain reconciles removals recorded before a restart — the
    exact incomplete mutations the orchestrator initiated, never a legitimate
    needs-human it does not own. This retries the external label removal ONLY —
    it never launches or relaunches a session (the investigation that superseded
    the stale marker is already running)."""
    for issue_number in session_launcher.needs_human_clear_store.pending_issue_numbers():
        _reconcile_stale_needs_human_clear(issue_number, session_launcher)


def _reconcile_stale_needs_human_clear(
    issue_number: int, session_launcher: SessionLauncher
) -> None:
    """Write-ahead the owed-clear record, attempt the removal, drop it on commit.

    Recording FIRST is deliberate: a crash anywhere after this decision then
    leaves a durable record a restart reconciles — with the provenance-unsafe
    inference removed, nothing else could recover a record lost in the window
    between a failed removal and a record-after-failure. ``record`` is
    idempotent, so a success-on-first-try records then discards (net empty) and
    the per-tick drain re-recording an already-owed issue is a no-op (#6771 r7)."""
    session_launcher.needs_human_clear_store.record(issue_number)
    committed = session_launcher.clear_needs_human_label(issue_number)
    if committed:
        session_launcher.needs_human_clear_store.discard(issue_number)
