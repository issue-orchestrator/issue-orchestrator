"""Terminating every terminal one issue owns, as one behaviour-level boundary.

Extracted from ``review_exchange_lifecycle`` to keep that module inside its
``oversized_control_hotspots`` budget. The policy here -- which terminals an
issue owns, custody before destruction, and complete-versus-partial teardown --
is the part entrypoints must not re-implement (#7255).
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Callable

from ..domain.validated_work_commands import ValidatedWorkDispositionBatch
from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired

if TYPE_CHECKING:
    from ..domain.models import Session
    from .review_exchange_lifecycle import (
        IssueRuntimeLifecycleOwners,
        IssueRuntimeTermination,
    )

logger = logging.getLogger(__name__)


class ValidatedWorkCustodyUnproven(RuntimeError):
    """Capture faulted, so NOTHING was torn down.

    Raised before any destructive step. It is the owner's way of saying the
    refusal is total, so callers can report that truthfully instead of guessing
    how far a teardown got (#7255).
    """


@dataclass(frozen=True, slots=True)
class IssueTerminationOutcome:
    """Result of terminating EVERY terminal an issue owns.

    `IssueRuntimeTermination` alone cannot answer this: `terminate` is
    ISSUE/REWORK-scoped by construction and builds its refs from the issue
    number, while a review terminal is `review-<PR number>`. Callers need to
    know both what stopped and whether anything survived, or they report
    "terminated" over a live agent.
    """

    termination: IssueRuntimeTermination
    #: Terminals this call actually stopped.
    stopped_session_ids: tuple[str, ...]
    #: Terminals settled WITHOUT being stopped, from either shape: a lingering
    #: record the issue-scoped boundary reconciled away, or a process that was
    #: already gone when we reached it. Both mean "not running, not killed by
    #: us"; neither is a failure.
    cleared_session_ids: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()

    @property
    def settled_session_ids(self) -> tuple[str, ...]:
        """Terminals that are no longer running: stopped, plus already-dead.

        The success signal, and NOT the same thing as `stopped_session_ids`.
        `_release_issue_runtime` routes a terminal whose record lingers after the
        process is gone to `cleared`, never to `stopped`, so judging success on
        `stopped` alone reports 500 for the most ordinary case there is -- the
        dashboard showing a session whose agent already exited.
        """
        return self.stopped_session_ids + self.cleared_session_ids

    @property
    def complete(self) -> bool:
        return not self.failures

    @property
    def failure_details(self) -> tuple[str, ...]:
        return tuple(f"{terminal_id}: {error}" for terminal_id, error in self.failures)


def capture_or_report(
    capture: Callable[[], ValidatedWorkDispositionBatch],
    *,
    issue_number: int,
    reason: str,
    events: EventSink,
) -> ValidatedWorkDispositionBatch | None:
    """Run an ALREADY-TERMINAL session's capture without letting it strand it.

    Opt-in, and narrowly. The one legitimate caller is
    `preserve_completed_terminal`: by then the agent has finished, its completion
    record is on disk and its branch is validated, so everything still to happen
    is bookkeeping -- drop the session from `active_sessions`, kill an idle
    terminal, write the labels. A fault there destroys nothing and reverts
    nothing; it just skips the bookkeeping, and the next tick rediscovers the
    session and does it all again. #7255 logged the same `ACTIVE -> COMPLETED`
    transition 218 times in two hours on this exact path and never wrote a label.

    Every teardown that can still DESTROY work stays strict and must not adopt
    this: `terminate` and `cancel_exchange` stop live terminals and release the
    exchange pair, `preserve`/`preserve_cleanup` run before a worktree is
    deleted, and `require_reset` gates a reset. A capture fault there means
    custody is unproven, so those refuse atomically instead -- see
    `test_merge_escalation_records_provenance_against_its_pr[True]`, which pins
    a corrupt-custody escalation to "no session stopped, no label written".
    """
    try:
        return capture()
    except (ReconciliationRequired, ClaimLostError):
        # Control flow, not a capture fault. `ActionApplier.apply` re-raises
        # these deliberately; swallowing them here would strand the very
        # reconciliation they exist to force.
        raise
    except Exception:
        logger.error(
            "[VALIDATED_WORK] capture failed for issue #%d (reason=%s); the "
            "session is already terminal, so terminalization continues without "
            "a disposition batch",
            issue_number,
            reason,
            exc_info=True,
        )
        events.publish(
            make_trace_event(
                EventName.VALIDATED_WORK_CAPTURE_FAILED,
                {"issue_number": issue_number, "reason": reason},
            )
        )
        # None, never `no_work(...)`. A fault is NOT "nothing to preserve":
        # `no_work` reports found_work=False and unresolved=False, which is this
        # repo's custody oracle answering "all clear" about a question that was
        # never asked. Callers must handle the absence explicitly.
        return None


def terminate_every_session(
    owners: "IssueRuntimeLifecycleOwners",
    issue_number: int,
    reason: str,
    *,
    capture: Callable[[], ValidatedWorkDispositionBatch],
    observe: Callable[[ValidatedWorkDispositionBatch], None],
) -> IssueTerminationOutcome:
    """Terminate every terminal this issue is KNOWN to own, whatever its type.

    "Known" is the honest bound: coverage is `issue-N`/`rework-N` by number plus
    anything holding an `active_sessions` record. A live terminal whose record
    was lost is not reachable from here -- `_reconcile_running_sessions` is what
    finds those.

    ``terminate`` alone covers ISSUE/REWORK plus the exchange pair and the
    publish retry, but an issue can also own ``review-<pr>``,
    ``retrospective-review-<n>`` and ``tech-lead-<n>`` terminals, which are
    identified only by their terminal id. Leaving that composition to a caller
    is how an operator Terminate came to orphan a live reviewer agent while
    labelling the issue blocked-failed (#7255).

    Custody is established ONCE, before anything is destroyed, and a fault
    raises `ValidatedWorkCustodyUnproven` with nothing touched. Each remaining
    terminal is preserved strictly before it is stopped, so one that cannot be
    preserved is reported as a failure rather than killed.

    `capture` and `observe` are injected by the owner rather than reached for,
    so this module composes the boundary without prying into it.
    """
    try:
        batch = capture()
    except (ReconciliationRequired, ClaimLostError):
        raise
    except Exception as exc:
        raise ValidatedWorkCustodyUnproven(
            f"validated-work custody could not be established for issue "
            f"#{issue_number}: {exc}"
        ) from exc

    termination = owners.core.release_preserved(issue_number, reason, batch)
    observe(batch)

    stopped = list(termination.stopped_session_ids)
    # `cleared_active_session_ids` means "record dropped", which INCLUDES every
    # terminal just stopped. Seeding from it directly double-counts the ordinary
    # `issue-N` terminal in `settled_session_ids`.
    already_stopped = set(stopped)
    cleared = [
        terminal_id for terminal_id in termination.cleared_active_session_ids
        if terminal_id not in already_stopped
    ]
    failures: list[tuple[str, str]] = []
    for session in _remaining_sessions(owners, issue_number, set(stopped) | set(cleared)):
        try:
            # Report what actually happened: a terminal already gone was CLEARED,
            # not killed, and `killed_sessions` is rendered back to the operator.
            (stopped if _stop_session(owners, session, reason) else cleared).append(
                session.terminal_id
            )
        # NOT re-raised here, unlike at the initial capture. A re-raise would
        # reach the route's `TerminationDeferred` handler, which answers
        # "Nothing was terminated" -- and by this point `release_preserved` has
        # already released the exchange pair, abandoned the publish retry and
        # stopped `issue-N`/`rework-N`. "Partially terminated" is the truthful
        # answer for anything that fails after that point.
        except Exception as exc:
            failures.append((session.terminal_id, str(exc) or type(exc).__name__))
    return IssueTerminationOutcome(
        termination, tuple(stopped), tuple(cleared), tuple(failures)
    )


def _remaining_sessions(
    owners: "IssueRuntimeLifecycleOwners", issue_number: int, settled: set[str]
) -> tuple["Session", ...]:
    """Sessions this issue still owns that the issue-scoped boundary missed.

    Returns the `Session`, not just its terminal id. The id alone forces a
    name-keyed rediscovery of run assets the caller already holds, and the
    ledger-wide sweep that rediscovery performs fails on unrelated issues'
    state -- turning an ordinary terminate into a false "may still be running"
    (#7255 review round 5).
    """
    seen: dict[str, "Session"] = {}
    for session in tuple(owners.core.active_sessions):
        if session.issue.number != issue_number:
            continue
        if session.terminal_id in settled:
            continue
        seen.setdefault(session.terminal_id, session)
    return tuple(seen.values())


def _stop_session(
    owners: "IssueRuntimeLifecycleOwners", session: "Session", reason: str
) -> bool:
    """Stop one terminal, preserving its exact run first. True if it was running.

    Liveness is checked BEFORE preserving: a terminal whose process is already
    gone cannot be killed, so skipping the preserve destroys nothing, and a
    preserve fault on it would otherwise be reported as "may still be running"
    when nothing was.
    """
    from .session_manager import SessionRef

    ref = SessionRef.from_name(session.terminal_id)
    # No None check, unlike `_release_issue_runtime`: that takes
    # `SessionManager | None`, while `CoreIssueRuntimeOwners.session_manager` is
    # non-optional, so pyright proves the branch dead.
    if not owners.core.session_manager.exists(ref):
        return False
    # Exact-run preserve, the contract `Orchestrator._kill_session` used: passing
    # `run` scopes the read to this one issue instead of sweeping the ledger.
    owners.preserve_terminal(
        session.issue.number, session.terminal_id, reason, run=session.run_assets
    )
    owners.core.session_manager.stop(ref)
    return True
