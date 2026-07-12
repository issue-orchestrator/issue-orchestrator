"""Crash-safe needs-human label-clear commit protocol for recovered triage launches.

When a queued failure investigation exhausts its bounded launch retries, the
orchestrator escalates it to needs-human by applying the source-of-truth label
first, then an explanatory comment. If the label lands but the comment fails,
``PendingTriageReview.needs_human_escalation_incomplete`` records that GitHub now
carries a stale needs-human marker. If a later tick's launch prep recovers and
the investigation launches, that marker contradicts the running work and must be
cleared.

The clear is a real external mutation and can fail — and the launch that owes it
can be lost to a crash the instant after ``launch_issue_session`` creates the
terminal, before anything durable records the owed clear. So the OBLIGATION is
committed through a phase-aware, launcher-owned :class:`NeedsHumanClearStore`
shared by the launch owner (this module's ``record_pending`` / ``clear`` /
``withdraw`` helpers, called from ``session_routing``) and the reconciliation
owner (``reconcile_needs_human_label_clears``):

1. ``record_pending`` writes a PROVISIONAL obligation BEFORE the terminal exists.
   Ownership is now durable no matter when a crash lands, but the removal waits:
   a launch that fails leaves the needs-human label legitimate.
2. A committed launch ``confirm``\\s the obligation and attempts the removal;
   because CONFIRMED is written durably first, a crash mid-clear leaves a record
   the per-tick reconciler retries. A failed launch ``withdraw``\\s the
   provisional obligation, so the legitimate label is never cleared.
3. On restart / per tick, ``reconcile_needs_human_label_clears`` retries every
   CONFIRMED removal and resolves every PROVISIONAL obligation the crash left
   dangling: confirm-and-clear if its investigation actually became a restored
   active session (the launch committed), withdraw otherwise.

Ownership is never inferred from "an active session still carries needs-human" —
a conjunction an operator's stop or a running session's own needs-human
transition also satisfies, which would strip a legitimate escalation (#6771
round 7). The durable record establishes ownership; the active-session signal is
used ONLY to disambiguate launch-committed vs launch-failed for a PROVISIONAL
obligation we already own.
"""

from ..domain.models import PendingTriageReview
from .session_launcher import SessionLauncher


def record_pending_needs_human_clear(
    triage: PendingTriageReview,
    session_launcher: SessionLauncher,
) -> None:
    """Write-ahead a PROVISIONAL clear obligation BEFORE the terminal exists (#6771 r8).

    Only for a triage whose prior tick left an incomplete escalation — i.e. the
    orchestrator itself applied a stale needs-human label it intends to clear if
    the investigation later launches. Writing the obligation before
    ``launch_issue_session`` creates the terminal is the whole point: a crash in
    the previously unowned gap between terminal creation and the confirm/clear
    then leaves a durable record a restart reconciles. The removal itself waits
    for a confirmed launch, so a launch that FAILS never clears a label that is
    still legitimate (the investigation never started)."""
    if not triage.needs_human_escalation_incomplete:
        return
    session_launcher.needs_human_clear_store.record_pending(triage.issue_number)


def clear_stale_needs_human_on_launch(
    triage: PendingTriageReview,
    session_launcher: SessionLauncher,
) -> None:
    """Confirm the provisional obligation and attempt the stale-label removal (#6771 r5-r8).

    A prior tick may have exhausted launch retries and applied the needs-human
    source-of-truth label but failed to commit the escalation (comment failed).
    Now that prep recovered and the investigation launched, that label is stale
    and contradicts the running work, so the successful-launch path promotes the
    obligation to CONFIRMED (durably, before the removal is attempted) and clears
    the label through the launcher's owning action boundary.

    The queued item is removed by the caller regardless (the session is running;
    it must not be relaunched), so the per-item flag cannot outlive it. If the
    removal does NOT commit, the CONFIRMED record survives so a later tick's
    reconciler retries the removal — and only the removal — and the record proves
    the orchestrator owns this clear across a restart (#6771 round 6/7/8)."""
    if not triage.needs_human_escalation_incomplete:
        return
    triage.needs_human_escalation_incomplete = False
    session_launcher.needs_human_clear_store.confirm(triage.issue_number)
    _attempt_confirmed_needs_human_clear(triage.issue_number, session_launcher)


def withdraw_pending_needs_human_clear(
    triage: PendingTriageReview,
    session_launcher: SessionLauncher,
) -> None:
    """Withdraw the provisional obligation when the launch did NOT commit (#6771 r8).

    A launch that failed before creating a restorable terminal leaves the
    needs-human label legitimate — the investigation never started — so the
    provisional obligation must never mature into a removal. Idempotent: a no-op
    when no obligation was written (the triage carried no incomplete escalation)."""
    if not triage.needs_human_escalation_incomplete:
        return
    session_launcher.needs_human_clear_store.withdraw(triage.issue_number)


def reconcile_needs_human_label_clears(
    session_launcher: SessionLauncher,
    active_issue_numbers: set[int],
) -> None:
    """Per-tick / post-restart drain of the launcher-owned clear store.

    Two phase-keyed passes over durable obligations the orchestrator owns:

    - CONFIRMED: re-attempt the owed label removal and drop it on commit. Because
      the store is durable this reconciles removals recorded before a restart —
      the exact incomplete mutations the orchestrator initiated, never a
      legitimate needs-human it does not own.
    - PROVISIONAL: written before a launch whose commit outcome a crash may have
      hidden. Resolve each against whether its investigation is now a restored
      active session — see :func:`_resolve_provisional_needs_human_clear`.

    On the first post-restart tick, ``active_issue_numbers`` is the rehydrated set
    of restored sessions, closing the crash gap between terminal creation and the
    confirm/clear. This retries the external label removal ONLY — it never
    launches or relaunches a session (any superseded investigation is already
    running)."""
    store = session_launcher.needs_human_clear_store
    for issue_number in store.confirmed_issue_numbers():
        _attempt_confirmed_needs_human_clear(issue_number, session_launcher)
    for issue_number in store.pending_issue_numbers():
        _resolve_provisional_needs_human_clear(
            issue_number, session_launcher, active_issue_numbers
        )


def _resolve_provisional_needs_human_clear(
    issue_number: int,
    session_launcher: SessionLauncher,
    active_issue_numbers: set[int],
) -> None:
    """Resolve a PROVISIONAL obligation a crash may have left mid-launch (#6771 r8).

    The provisional record was durably written BEFORE ``launch_issue_session``
    created the terminal, so on restart it outlives a crash that landed in the
    previously unowned gap between terminal creation and the confirm/clear. Its
    OWNERSHIP is already proven by the record; the only open question is whether
    the launch COMMITTED, answered by one deliberately narrow signal: whether the
    crashed process's investigation was restored as an active session.

    This is NOT the reverted round-7 inference. Round 7 tried to DERIVE ownership
    from "an active session still carries needs-human", which an operator stop or
    a running session's own needs-human transition also satisfies. Here ownership
    is NOT inferred — it is established by the durable provisional record the
    orchestrator itself wrote. This check reads no label; it only decides
    launch-committed vs launch-failed for an obligation we already own, and never
    manufactures an obligation from active sessions.

    - restored/active -> the launch committed: CONFIRM, then clear the label.
    - not restored    -> the launch never committed: WITHDRAW (label legitimate)."""
    if issue_number in active_issue_numbers:
        session_launcher.needs_human_clear_store.confirm(issue_number)
        _attempt_confirmed_needs_human_clear(issue_number, session_launcher)
    else:
        session_launcher.needs_human_clear_store.withdraw(issue_number)


def _attempt_confirmed_needs_human_clear(
    issue_number: int, session_launcher: SessionLauncher
) -> None:
    """Attempt the owed removal for a CONFIRMED obligation; drop it on commit.

    The obligation is already durably CONFIRMED before this runs, so a crash
    anywhere here leaves a record the per-tick drain retries (write-ahead
    preserved). ``clear_needs_human_label`` is idempotent — an already-absent
    label reports committed — so a success drops the record and a self-healed
    removal needs no special case (#6771 r6-r8)."""
    committed = session_launcher.clear_needs_human_label(issue_number)
    if committed:
        session_launcher.needs_human_clear_store.withdraw(issue_number)
