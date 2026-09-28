"""Shared result types for session launch flows."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from typing import TYPE_CHECKING

from ..domain.models import Session
from ..ports.repository_host import HostRateLimit, host_rate_limit_of

if TYPE_CHECKING:
    from .action_base import Action
    from .action_results import ActionResult


#: The review-skip reason a review held by the recovery owner reports: a
#: wait, not a refusal (the exam's livelock detector counts it as waiting).
REVIEW_HELD_BY_RECOVERY = "held_by_recovery"


class LaunchDisposition(Enum):
    """What a launch attempt means for the pending item that requested it.

    A failed launch is not one thing. "The terminal is already up", "I could
    not read the file I needed", "the provider refused" and "give up" call for
    four different queue reactions, and encoding them as ad-hoc booleans meant
    an unrecognised failure silently fell through to the most destructive one —
    dropping the work (#6999 F10/A1). Every launch path returns exactly one of
    these, and one owner maps it to a queue action.
    """

    #: A session started. The pending item is done.
    LAUNCHED = "launched"
    #: A terminal for this work is already running. The queue keeps the item
    #: and tries to restore that terminal; a successful restore consumes it.
    EXISTING_TERMINAL = "existing_terminal"
    #: The provider refused before anything was attempted — an expired login,
    #: or a CLI that is not installed. Nothing about the work failed and
    #: nothing was consumed, so the item stays queued exactly as it was, for a
    #: tick when the provider is ready. Deliberately distinct from a retry
    #: budget: there is no failure here to count against the work.
    PROVIDER_DEFERRED = "provider_deferred"
    #: The launch attempt itself failed and may work next time: required input
    #: could not be prepared (a transient DB/log/filesystem read), or the
    #: terminal never came up. The item is retained, but on a bounded retry
    #: budget owned by the queue — unlike a provider refusal, this attempt DID
    #: fail, and an input or a terminal that never recovers must not relaunch
    #: forever. Named for the retry rather than for one of its causes (#6999
    #: F5): a failed ``create_session`` is the same decision for the queue as a
    #: failed input read, and calling it an input failure would have made
    #: routing it here a lie.
    RETRYABLE_FAILURE = "retryable_failure"
    #: The repository host refused on a rate limit that resets at a known
    #: instant (#7297). Like a provider refusal, nothing about the WORK failed,
    #: so the item stays queued with its budget untouched; unlike one, the wait
    #: has a deadline, which :class:`HostRateLimitLaunchGate` holds every
    #: launch to. It is also the only deferral with a bound: a limit that never
    #: lifts is turned back into a ``RETRYABLE_FAILURE`` by that gate, so the
    #: work still reaches a human eventually instead of waiting forever.
    HOST_RATE_LIMITED = "host_rate_limited"
    #: The durable pending-work claim could not be recorded, so the launch
    #: never happened AND nothing about this request exists in the ledger
    #: (#6999 F1 round 2). Deliberately distinct from ``RETRYABLE_FAILURE``,
    #: which it used to borrow: that disposition spends a unit of the queue's
    #: bounded budget, and the settlement makes that spend durable by rewriting
    #: the deferred row - a row that, in this case, was never created. The
    #: rewrite silently matched zero rows, so the budget was spent in memory
    #: against nothing, and a process death then lost the request outright.
    #: Nothing failed about the WORK here; the ledger did. The item is retained
    #: with its budget untouched, exactly as a provider refusal leaves it.
    CLAIM_UNRECORDED = "claim_unrecorded"
    #: The request is no longer wanted and nothing failed (#7455): its subject
    #: moved on - a queued review whose issue or PR is now blocked, closed or
    #: sent back for rework, or work that is already running in a session. The
    #: queue drops the item, exactly as for a permanent failure, but the plan
    #: step is a withdrawal, not a failed launch.
    WITHDRAWN = "withdrawn"
    #: The subject is held by the validated-work recovery owner (#7455): the
    #: issue carries ``recovery-pending`` and that hold is ALL that keeps the
    #: review from launching. That owner routes the published work to review
    #: and then releases the hold, so the item stays queued untouched, with no
    #: budget spent, and launches once the hold is gone. Any other block still
    #: withdraws it.
    HELD_BY_RECOVERY = "held_by_recovery"
    #: The launcher gave up. The queue drops the item.
    PERMANENT_FAILURE = "permanent_failure"


@dataclass
class LaunchResult:
    """Result of a session launch attempt."""

    session: Session | None
    success: bool
    reason: str = ""
    #: How the owning queue should settle its pending item. Defaults to
    #: ``PERMANENT_FAILURE`` so a launch path that fails without saying why is
    #: treated as the launcher having given up — the historical behaviour — and
    #: is normalised to ``LAUNCHED`` whenever the launch actually succeeded.
    disposition: LaunchDisposition = LaunchDisposition.PERMANENT_FAILURE
    #: Present exactly when the disposition is ``HOST_RATE_LIMITED``: the typed
    #: reset the deferral waits for, never re-derived from ``reason`` text.
    host_rate_limit: HostRateLimit | None = None
    #: Present exactly when the disposition is ``EXISTING_TERMINAL``: the name
    #: of the terminal that is actually running. A caller never re-derives it:
    #: since #7347 a kind can find its work under more than one name (a tech
    #: lead launched before the upgrade runs as ``issue-N``).
    existing_terminal: str | None = None

    def __post_init__(self) -> None:
        if self.success:
            self.disposition = LaunchDisposition.LAUNCHED
        rate_limited = self.disposition is LaunchDisposition.HOST_RATE_LIMITED
        if rate_limited != (self.host_rate_limit is not None):
            raise ValueError(
                "a HOST_RATE_LIMITED launch result must carry its host rate "
                "limit, and no other result may"
            )
        existing = self.disposition is LaunchDisposition.EXISTING_TERMINAL
        if existing != (self.existing_terminal is not None):
            raise ValueError(
                "an EXISTING_TERMINAL launch result must name the running "
                "terminal, and no other result may"
            )

    @classmethod
    def terminal_already_running(cls, terminal: str) -> "LaunchResult":
        """A terminal for this work is already running under ``terminal``."""
        return cls(
            None,
            False,
            "Terminal session already running",
            disposition=LaunchDisposition.EXISTING_TERMINAL,
            existing_terminal=terminal,
        )

    @classmethod
    def terminal_spawn_failed(cls) -> "LaunchResult":
        """The terminal never came up, on any launch path (#6999 F5).

        A factory rather than five hand-built results, because every launch
        path has to agree on what a failed ``create_session`` means and two of
        them did not: review and rework returned SUCCESS, handing back a
        phantom session for a terminal that does not exist. One constructor is
        what makes "did the terminal start?" impossible to answer differently
        per queue.

        The disposition is deliberately RETRYABLE rather than permanent:
        nothing about the request failed, so the queue keeps it on its bounded
        budget instead of destroying a review, rework or investigation because
        the terminal manager hiccuped once.
        """
        return cls(
            None,
            False,
            "Failed to create terminal session",
            disposition=LaunchDisposition.RETRYABLE_FAILURE,
        )

    @classmethod
    def required_input_unavailable(cls, reason: str) -> "LaunchResult":
        """Retain queued work when required launch input cannot be prepared."""
        return cls(
            None,
            False,
            f"Required launch input unavailable: {reason}",
            disposition=LaunchDisposition.RETRYABLE_FAILURE,
        )

    @classmethod
    def host_rate_limited(
        cls, reason: str, rate_limit: HostRateLimit
    ) -> "LaunchResult":
        """The host refused until ``rate_limit.resets_at``; defer, spend nothing."""
        return cls(
            None,
            False,
            reason,
            disposition=LaunchDisposition.HOST_RATE_LIMITED,
            host_rate_limit=rate_limit,
        )

    @classmethod
    def input_preparation_failed(cls, what: str, error: Exception) -> "LaunchResult":
        """A required launch input could not be prepared (#7297).

        A rate limit anywhere behind ``error`` is a deferral with a reset, not
        a failure: classifying it here is what keeps a launch that CATCHES its
        preparation errors from spending the retry budget on a request GitHub
        already said it would refuse.
        """
        reason = f"{what}: {error}"
        rate_limit = host_rate_limit_of(error)
        if rate_limit is not None:
            return cls.host_rate_limited(reason, rate_limit)
        return cls(
            None, False, reason, disposition=LaunchDisposition.RETRYABLE_FAILURE
        )

    @property
    def defers_to_provider(self) -> bool:
        """Whether the provider refused and the work must stay untouched."""
        return self.disposition is LaunchDisposition.PROVIDER_DEFERRED


@dataclass
class ClaimAcquisitionResult:
    """Result of attempting to acquire a distributed claim for an issue.

    Used to track claim state through the launch process so cleanup
    can release claims on failure.
    """

    success: bool
    lease_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None
    host_rate_limit: HostRateLimit | None = None

    def as_launch_failure(self) -> LaunchResult:
        """Convert a failed claim to a launch result; a rate limit defers (#7297)."""
        reason = self.error or "Claim acquisition failed"
        if self.host_rate_limit is not None:
            return LaunchResult.host_rate_limited(reason, self.host_rate_limit)
        return LaunchResult(None, False, reason)


class LaunchStepOutcome(Enum):
    """What one planned launch step came to, as the plan applier must report it.

    A launch path settles its queue from a :class:`LaunchDisposition`; the
    action applier needs a coarser answer, and needs it typed: before #7455 it
    only saw "a session, or ``None``", so a queued review that was deliberately
    withdrawn - or was never queued at all - was applied as a failed launch.
    """

    #: A session is running this work: started now, or an existing terminal
    #: adopted.
    LAUNCHED = "launched"
    #: Nothing to launch, and nothing failed: the request was withdrawn (see
    #: ``LaunchDisposition.WITHDRAWN``), or no such request was queued.
    WITHDRAWN = "withdrawn"
    #: Nothing launched yet, and nothing failed: the request waits on its
    #: queue for an owner to release its subject.
    WAITING = "waiting"
    #: The launch was attempted and did not start a session.
    NOT_LAUNCHED = "not_launched"


@dataclass(frozen=True)
class LaunchStep:
    """The typed result of one launch step, handed back to the action applier."""

    outcome: LaunchStepOutcome
    session: Session | None
    reason: str

    def __post_init__(self) -> None:
        if (self.outcome is LaunchStepOutcome.LAUNCHED) != (self.session is not None):
            raise ValueError("exactly a LAUNCHED step carries its session")

    @classmethod
    def launched(cls, session: Session) -> "LaunchStep":
        return cls(LaunchStepOutcome.LAUNCHED, session, "session launched")

    @classmethod
    def withdrawn(cls, reason: str) -> "LaunchStep":
        return cls(LaunchStepOutcome.WITHDRAWN, None, reason)

    @classmethod
    def not_queued(cls, kind: str, number: int) -> "LaunchStep":
        """No request for ``number`` is queued: it already launched or left."""
        return cls.withdrawn(f"no {kind} is queued for #{number}")

    @classmethod
    def not_launched(cls, reason: str) -> "LaunchStep":
        return cls(LaunchStepOutcome.NOT_LAUNCHED, None, reason)

    @classmethod
    def of_session(cls, session: Session | None, reason: str) -> "LaunchStep":
        """For a launch path that reports only a session: none means not launched."""
        return cls.launched(session) if session is not None else cls.not_launched(reason)

    @classmethod
    def of_result(cls, result: LaunchResult) -> "LaunchStep":
        """The step a launch result comes to when no terminal was adopted."""
        if result.success and result.session is not None:
            return cls.launched(result.session)
        if result.disposition is LaunchDisposition.WITHDRAWN:
            return cls.withdrawn(result.reason)
        if result.disposition is LaunchDisposition.HELD_BY_RECOVERY:
            return cls(LaunchStepOutcome.WAITING, None, result.reason)
        return cls.not_launched(result.reason or result.disposition.value)


def launch_step_result(action: "Action", step: LaunchStep, failure: str) -> "ActionResult":
    """The applied result of a launch step that started no session (#7455).

    A withdrawn or waiting request is a SKIPPED step - nothing failed, so it
    is not an ``apply.failed``, marks nothing failed this cycle, and spends
    nothing. Only a launch that was attempted and did not start is a failure.
    """
    from .action_results import ActionResult

    if step.outcome is LaunchStepOutcome.LAUNCHED:
        raise ValueError("a launched step is applied as a success, not here")
    if step.outcome is LaunchStepOutcome.NOT_LAUNCHED:
        return ActionResult.fail(action, failure)
    return ActionResult.skip(action, step.reason, launch_step=step.outcome.value)
