"""One session's completion failure stays that session's (#8000, #8693).

The tick handles every active session in one pass: observe it, decide its
outcome, apply the decision. An exception from any one of those steps used to
escape the pass, so the WHOLE iteration aborted - no other session's
completion, no planning, no launch for any issue - and the next tick hit the
same session again (#8000: one missing run dir, 902 aborted ticks). A rework's
``needs_human`` on a PR_PENDING issue did it once per occurrence (#8693).

This owner confines such a failure to its session through the action liveness
owner (#7350), keyed on the session's run:

* a failure that leaves the session in the pass (observe, decide, or an
  apply that raised before the session left ``active_sessions``) is retried
  next tick, so it records the error's own outcome: backoff, and a park once
  its budget is spent;
* a failure after the session left the pass is not retried by anything - the
  steps it did not run are a person's to finish - so it records a permanent
  outcome and parks on the spot.

A park escalates through the liveness owner (the shared needs-human block and
a comment on the issue) and, while it stands, :meth:`admit` keeps the session
out of the pass and :meth:`hold_unfinished` keeps it from being retired.
Engine-wide faults are not this owner's: anything that is not an
:class:`Exception`, and a confirmed-permanent issue fetch failure, still reach
the loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    LivenessKey,
    fact_fingerprint,
)
from .issue_fetch_resilience import PermanentIssueFetchError
from .planned_action_liveness import outcome_of_error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from ..domain.models import Session
    from ..observation.observation import SessionObservationResult
    from .action_liveness import ActionLivenessOwner
    from .completion_dispatcher import CompletedDecision

logger = logging.getLogger(__name__)

#: The liveness action every session's completion pass is recorded under.
COMPLETE_SESSION = "complete_session"


@dataclass(frozen=True)
class CompletionContainment:
    """Admits each session into the completion pass and confines its failures."""

    owner: "ActionLivenessOwner"

    @staticmethod
    def key(session: "Session") -> LivenessKey:
        """The session's completion, keyed on its run: a later run is new facts."""
        issue = session.issue.number
        return LivenessKey(
            identity=ActionIdentity(subject=f"issue:{issue}", action=COMPLETE_SESSION),
            fingerprint=fact_fingerprint(session.run_assets.identity),
            escalation_issue=issue if issue > 0 else None,
        )

    @staticmethod
    def confines(error: BaseException) -> bool:
        """Whether ``error`` is one session's, rather than the engine's."""
        return isinstance(error, Exception) and not isinstance(error, PermanentIssueFetchError)

    def hold_unfinished(self) -> None:
        """Keep every parked completion standing until it is settled (#8693 r2).

        A failed apply parks after its session has left the pass, so nothing
        else asks about that row again. Called once per pass, this keeps the
        liveness owner from retiring it as abandoned (or superseded by a later
        run's success) and withdrawing its block while the completion's
        remaining steps are still undone. A person's Retry/Dismiss of the
        escalation, or an operator release, settles it.
        """
        self.owner.keep_asking(COMPLETE_SESSION)

    def admit(self, session: "Session") -> bool:
        """May the pass handle ``session`` this tick? Not while it backs off or is parked."""
        decision = self.owner.admit(self.key(session))
        if not decision.admitted:
            logger.info(
                "[COMPLETION] Holding %s (issue #%d) out of the completion pass: %s",
                session.terminal_id, session.issue.number, decision.describe(),
            )
        return decision.admitted

    def settled(self, session: "Session") -> None:
        """The session's decision applied: any failure it had recorded is resolved."""
        self.owner.record(self.key(session), ActionOutcome.done())

    def observe(
        self, session: "Session", observe: "Callable[[Session], SessionObservationResult]"
    ) -> "SessionObservationResult | None":
        """``observe(session)``; ``None`` when it failed and was confined."""
        try:
            return observe(session)
        except Exception as exc:
            if not self.confines(exc):
                raise
            self.contain(session, exc, retried=True)
            return None

    def apply_each(
        self,
        completed_decisions: "Iterable[CompletedDecision]",
        apply: "Callable[[CompletedDecision], None]",
        *,
        in_pass: "Callable[[Session], bool]",
    ) -> None:
        """Apply every drained decision; confine each session's failure to it.

        ``in_pass(session)`` says, after a failure, whether the session is
        still active - so the next tick retries it - whatever step raised: a
        decide error, or an apply that failed before the session left the pass
        (#8693 r3). Only an engine-wide fault is raised, after every sibling
        has been applied.
        """
        errors: list[BaseException] = []
        for completed in completed_decisions:
            try:
                apply(completed)
            except Exception as exc:
                if not self.confines(exc):
                    errors.append(exc)
                    continue
                self.contain(completed.session, exc, retried=in_pass(completed.session))
            except BaseException as exc:
                errors.append(exc)
            else:
                self.settled(completed.session)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("completion decision apply failures", errors)

    def contain(self, session: "Session", error: Exception, *, retried: bool) -> None:
        """Record ``error`` against ``session`` instead of aborting the tick.

        ``retried``: the session is still in the pass and the next tick runs
        it again. Otherwise the failure is permanent and parks now.
        """
        logger.error(
            "[COMPLETION] Completion of %s (issue #%d) failed; confined to this session"
            " (#8000): %s: %s",
            session.terminal_id, session.issue.number, type(error).__name__, error,
            exc_info=error,
        )
        outcome = (
            outcome_of_error(error)
            if retried
            else ActionOutcome.permanent(
                f"completing {session.terminal_id} raised {type(error).__name__}: {error}"
                " after it left the completion pass; the steps after it did not run"
            )
        )
        self.owner.record(self.key(session), outcome)


__all__ = ["COMPLETE_SESSION", "CompletionContainment"]
