"""Operator-initiated whole-issue termination: teardown, hold, and history.

Extracted from the HTTP handler. A route should not decide which terminals an
issue owns, what happens to runtime state when a teardown is only partial, or
which hold labels a termination writes -- that is policy, and it drifted from
the owner that produces the termination outcome (#7255).

It sits in `entrypoints/` rather than `control/` only because applying labels
goes through `execution.label_ops`, which `control` may not import.

DEFERRED (CLAUDE.md "Final Abstraction Pass" decision rule):
  Scope:     move the label/history/state sequence behind
             `IssueRuntimeLifecycleOwners`, so the whole operator termination is
             one owner call and `bulk_kill`, the reset-retry wiring and this
             route cannot drift apart.
  Blocked by: the `control` -> `execution.label_ops` layer boundary; it needs a
             label-application port before the move is legal.
  Owner:     BruceBGordon
  Due:       milestone M-next (the first milestone opened after this merges)
  Risk:      LOW while there is a single caller. If a second caller appears
             before the move, it must reuse `terminate_issue_and_hold` rather
             than re-compose the steps -- re-composing them is exactly how this
             route came to miss review terminals (#7255).
  Follow-up: filed against this PR; see the PR description for the issue link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from ..control.claim_gate import ClaimLostError
from ..control.label_manager import LabelManager
from ..control.reconciliation import ReconciliationRequired
from ..control.review_exchange_lifecycle import ValidatedWorkCustodyUnproven
from ..execution.label_ops import LabelOperation, apply_label_operations

logger = logging.getLogger(__name__)


class TerminationRefused(Exception):
    """Custody could not be established, so nothing was torn down.

    The owner refuses atomically when validated-work capture faults -- killing a
    terminal whose work was never preserved is how validated work gets lost. The
    operator still has to be TOLD that: before #7255 the refusal escaped as an
    unhandled 500 with no body, which reads from the UI as "Terminate does
    nothing".
    """


class TerminationIncomplete(Exception):
    """Teardown faulted AFTER custody was established.

    Distinct from `TerminationRefused` on purpose: by this point the owner may
    have released the exchange pair or stopped terminals, so telling the
    operator "nothing was terminated" would be a lie.
    """


class TerminationDeferred(Exception):
    """The issue must reconcile before it can be terminated.

    `ReconciliationRequired`/`ClaimLostError` are control flow the applier
    re-raises on purpose, but there is no tick loop above an HTTP handler -- so
    letting them escape here is a bodyless 500 on the operator's button, the
    exact symptom this change exists to remove.
    """


@dataclass(frozen=True, slots=True)
class OperatorTermination:
    """What an operator Terminate actually achieved.

    Typed rather than a raw dict so the route cannot confuse "stopped" with
    "settled": a terminal whose process had already exited is reconciled as
    cleared, never stopped, and judging success on the wrong one returns 500 for
    an ordinary terminate (#7255).
    """

    killed_sessions: tuple[str, ...]
    settled_sessions: tuple[str, ...]
    errors: tuple[str, ...]
    complete: bool
    hold_label: str


def terminate_issue_and_hold(
    orchestrator: Any, issue_number: int, sessions: list[Any], *, lm: LabelManager
) -> OperatorTermination:
    """Terminate running sessions and apply a hold guard to prevent auto-requeue."""
    from ..domain.models import SessionHistoryEntry

    state = orchestrator.state
    repo = orchestrator.repository_host

    killed_sessions: list[str] = []
    pr_numbers = sorted(
        {
            int(s.pr_number)
            for s in sessions
            if getattr(s, "pr_number", None) is not None
        }
    )

    # ONE owner call. Which terminals an issue owns, the order they come down
    # in, and whether anything survived are all policy, and policy does not
    # belong in an HTTP handler -- that is how this route came to miss review
    # terminals entirely (#7255). Custody is established inside, before
    # anything is destroyed, and a fault raises with nothing torn down.
    try:
        outcome = orchestrator.terminate_every_session_for_issue(
            issue_number, reason="operator-terminated"
        )
    except ValidatedWorkCustodyUnproven as refusal:
        logger.error(
            "[terminate] refused for issue #%d: validated-work custody could not "
            "be established; nothing was torn down", issue_number, exc_info=True,
        )
        raise TerminationRefused(str(refusal) or type(refusal).__name__) from refusal
    except (ReconciliationRequired, ClaimLostError) as exc:
        # Real conditions with a real answer, not something to escape an HTTP
        # handler as a bodyless 500: there is no tick loop above this frame.
        logger.warning(
            "[terminate] issue #%d must reconcile before it can be terminated: %s",
            issue_number, exc,
        )
        raise TerminationDeferred(str(exc) or type(exc).__name__) from exc
    except Exception as exc:
        logger.error(
            "[terminate] issue #%d: teardown faulted after custody was "
            "established; the issue may be partially terminated",
            issue_number, exc_info=True,
        )
        raise TerminationIncomplete(str(exc) or type(exc).__name__) from exc

    killed_sessions.extend(outcome.stopped_session_ids)
    errors = list(outcome.failure_details)

    # Keep tracking anything the owner reported as STILL RUNNING. Wiping its
    # active-session row would tell the operator "may still be running" in the
    # same breath as deleting the only record of it -- the agent goes invisible
    # to the dashboard and a retry 404s on the session lookup (#7255 review).
    state.release_issue(
        issue_number,
        keep_terminals=frozenset(terminal_id for terminal_id, _ in outcome.failures),
    )
    _append_operator_termination_history(
        state=state,
        issue_number=issue_number,
        primary_session=sessions[0],
        session_entry_cls=SessionHistoryEntry,
        now=datetime.now(timezone.utc),
    )

    # Label policy:
    # - issue: add blocked-failed guard, remove in-progress/pr-pending
    # - linked PR(s): add blocked-failed and remove needs-rework (scanner trigger)
    label_ops: list[LabelOperation] = [
        LabelOperation("add", issue_number, lm.blocked_failed),
        LabelOperation("remove", issue_number, lm.in_progress),
        LabelOperation("remove", issue_number, lm.pr_pending),
    ]
    for pr_number in pr_numbers:
        label_ops.extend(
            [
                LabelOperation("add", pr_number, lm.blocked_failed),
                LabelOperation("remove", pr_number, lm.needs_rework),
            ]
        )
    apply_label_operations(
        repo,
        label_ops,
        logger=logger,
        log_prefix="[terminate]",
    )

    return OperatorTermination(
        killed_sessions=tuple(killed_sessions),
        # Stopped PLUS already-dead: the success signal. A terminal whose process
        # had already exited is reconciled as `cleared`, never `stopped`, so
        # judging on `killed_sessions` alone 500s the most ordinary terminate
        # there is (#7255 review, BLOCKER 1).
        settled_sessions=outcome.settled_session_ids,
        errors=tuple(errors),
        complete=outcome.complete,
        hold_label=lm.blocked_failed,
    )


def _resolve_agent_label(primary_session: Any) -> str:
    agent_label = primary_session.agent_label
    if agent_label:
        return str(agent_label)
    for label in primary_session.issue.labels:
        if label.startswith("agent:"):
            return label
    return "agent:unknown"


def _append_operator_termination_history(
    *,
    state: Any,
    issue_number: int,
    primary_session: Any,
    session_entry_cls: Any,
    now: Any,
) -> None:
    state.record_operator_termination(
        issue_number,
        session_entry_cls(
            issue_number=issue_number,
            title=primary_session.issue.title,
            agent_type=_resolve_agent_label(primary_session),
            status="blocked",
            runtime_minutes=primary_session.runtime_minutes,
            status_reason="Terminated by operator",
            worktree_path=primary_session.worktree_path,
            completed_at=now,
            issue_labels=tuple(primary_session.issue.labels),
        ),
    )


