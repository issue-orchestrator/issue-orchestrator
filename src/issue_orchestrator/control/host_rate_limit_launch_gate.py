"""The one owner of "the host is rate limited" for every launch path (#7297).

A GitHub rate limit used to reach launch policy as a generic failure. Tech-lead
preparation caught it and reported ``RETRYABLE_FAILURE``, so each tick spent
one of three attempts on a request GitHub had already said it would refuse, and
the third turned a transient token limit into a needs-human escalation that
then silenced every later health review. The other launch paths let the error
escape the launcher entirely and simply hit GitHub again next tick.

Every launch now passes through :meth:`HostRateLimitLaunchGate.launch`, which
answers the question the same way for all of them:

* while the shared :class:`~..domain.host_rate_limit.HostRateLimitWindow` is
  open, the launch is not attempted at all - it is deferred to the reset;
* a rate limit raised out of the launch, or returned by one that caught its
  own preparation error, becomes a ``HOST_RATE_LIMITED`` result: retained,
  no budget spent, and the window opened until the reset;
* each deferral is published as ``SESSION_LAUNCH_DEFERRED_RATE_LIMIT`` on the
  issue's timeline, so waiting reads differently from failing;
* once the limit has held without a break past
  :data:`~..domain.host_rate_limit.RATE_LIMIT_DEFERRAL_BOUND`, the attempt is
  handed back as the ``RETRYABLE_FAILURE`` it used to be, so a token that is
  limited forever still reaches a human through the queue's own budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections.abc import Sequence
from typing import TYPE_CHECKING, Callable

from ..domain.host_rate_limit import (
    RATE_LIMIT_DEFERRAL_BOUND,
    HostRateLimit,
    HostRateLimitWindow,
    RateLimitEpisode,
)
from ..events import EventName
from ..ports import EventSink
from ..ports.event_sink import make_trace_event
from ..ports.repository_host import host_rate_limit_of
from .session_launch_types import LaunchDisposition, LaunchResult

if TYPE_CHECKING:
    from ..ports.claim_manager import ClaimManager
    from .action_base import Action
    from .session_launch_types import ClaimAcquisitionResult
    from .action_results import ActionResult
    from .planner_types import OrchestratorSnapshot, SkippedItem

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HostRateLimitLaunchGate:
    """Defer launches on the host's rate limit; never count the wait as failure."""

    window: HostRateLimitWindow
    events: EventSink
    clock: Callable[[], datetime] = field(default=lambda: _utc_now())

    def launch(
        self,
        attempt: Callable[[], LaunchResult],
        *,
        issue_number: int | None,
        work: str,
    ) -> LaunchResult:
        """Run ``attempt`` unless the window is open, classifying a rate limit.

        ``work`` names the launch path (a pending-work kind, or ``issue``) for
        the timeline event only. ``issue_number`` is ``None`` only for a rework
        whose issue cannot be resolved, which its launcher refuses before any
        GitHub read.
        """
        holding = self.window.open_at(self.clock())
        if holding is not None:
            # Not attempted, so not an observation: the episode and its bound
            # advance only when the host actually refuses again.
            self._publish(
                holding, issue_number=issue_number, work=work, attempted=False
            )
            if holding.bound_exceeded:
                # Refused just as surely as an attempt would be, and past the
                # bound every refusal counts - or a path that only ever meets
                # the open window (the tech-lead authority's own read opened
                # it) would never reach the budget.
                return _past_bound(
                    f"GitHub rate limit ({holding.limit.kind}) holds launches until "
                    f"{holding.limit.resets_at.isoformat()}",
                    holding,
                )
            return LaunchResult.host_rate_limited(
                f"GitHub rate limit ({holding.limit.kind}) holds launches until "
                f"{holding.limit.resets_at.isoformat()}",
                holding.limit,
            )
        try:
            result = attempt()
        except Exception as exc:
            limit = host_rate_limit_of(exc)
            if limit is None:
                raise
            result = LaunchResult.host_rate_limited(
                f"Launch refused by a GitHub rate limit: {exc}", limit
            )
        if result.host_rate_limit is None:
            if result.success:
                # Positive evidence the host answers again: only this ends an
                # episode, so the bound cannot be dodged by a late tick.
                self.window.recovered()
            return result
        episode = self.observe(
            result.host_rate_limit, issue_number=issue_number, work=work
        )
        if not episode.bound_exceeded:
            return result
        return _past_bound(result.reason, episode)

    def observe(
        self, limit: HostRateLimit, *, issue_number: int | None, work: str
    ) -> RateLimitEpisode:
        """Record a refusal the host just made, and announce the deferral.

        Public for the launch-adjacent reads that catch their own errors (the
        tech-lead launch authority's subject revalidation): they must open the
        same window, or the planner would keep asking GitHub every tick.
        """
        episode = self.window.observe(limit, self.clock())
        self._publish(episode, issue_number=issue_number, work=work, attempted=True)
        log = logger.error if episode.bound_exceeded else logger.warning
        log(
            "[GITHUB] %s launch for #%s refused by a %s rate limit (%s); "
            "deferring every launch until %s (limited for %d min%s)",
            work,
            issue_number,
            limit.kind,
            limit.resource or "unnamed resource",
            episode.limit.resets_at.isoformat(),
            _minutes(episode),
            ", PAST the deferral bound" if episode.bound_exceeded else "",
        )
        return episode

    def _publish(
        self,
        episode: RateLimitEpisode,
        *,
        issue_number: int | None,
        work: str,
        attempted: bool,
    ) -> None:
        self.events.publish(make_trace_event(
            EventName.SESSION_LAUNCH_DEFERRED_RATE_LIMIT,
            {
                "issue_number": issue_number,
                "work": work,
                "resets_at": episode.limit.resets_at.isoformat(),
                "limit_kind": episode.limit.kind,
                "resource": episode.limit.resource,
                "limited_since": episode.limited_since.isoformat(),
                "limited_for_seconds": int(episode.limited_for.total_seconds()),
                "attempted": attempted,
                "retry_budget_spent": episode.bound_exceeded,
            },
        ))


@dataclass(frozen=True)
class LaunchMutations:
    """How a launch's own GitHub writes went, typed rather than a bare bool.

    A launch that could not write its in-progress label because GitHub
    rate-limited the write has not failed any more than one whose preparation
    read was refused (#7297); ``refused`` keeps the two apart.
    """

    ok: bool
    host_rate_limit: HostRateLimit | None = None

    def refused(self, reason: str) -> LaunchResult:
        """The launch result for a failed batch: deferred on a rate limit."""
        if self.host_rate_limit is not None:
            return LaunchResult.host_rate_limited(reason, self.host_rate_limit)
        return LaunchResult(None, False, reason)


def apply_launch_mutations(
    apply: Callable[["Action"], "ActionResult"],
    actions: Sequence["Action"],
    *,
    context: str,
) -> LaunchMutations:
    """Apply every mutation; report failure with any host rate limit behind it."""
    ok, limit = True, None
    for action in actions:
        result = apply(action)
        if result.success:
            continue
        ok = False
        limit = limit or result.host_rate_limit
        logger.warning(
            "[launch] Failed to apply %s (%s): %s",
            action.action_type.value,
            context,
            result.error,
        )
    return LaunchMutations(ok, limit)


def converge_claim(
    claims: "ClaimManager", issue_number: int, lease_id: str
) -> "bool | ClaimAcquisitionResult":
    """Confirm a just-taken claim, or defer when the host is rate limited.

    The claim store cannot be asked before the reset, so the claim is handed
    back (best effort: the release is itself a request, and the lease expires
    anyway) and the launch defers instead of reading "another claimant won".
    """
    from .session_launch_types import ClaimAcquisitionResult

    try:
        return claims.run_convergence(issue_number, lease_id)
    except Exception as exc:
        limit = host_rate_limit_of(exc)
        if limit is None:
            raise
        refusal = str(exc)
    try:
        claims.release_claim(issue_number, lease_id)
    except Exception as release_error:
        logger.warning(
            "[GITHUB] #%d: could not release a rate-limited claim; its lease "
            "expires on its own: %s",
            issue_number,
            release_error,
        )
    return ClaimAcquisitionResult(
        success=False,
        error=f"Claim convergence refused by a GitHub rate limit: {refusal}",
        host_rate_limit=limit,
    )


#: The one deferral reason a rate-limited tick records, stable across ticks so
#: the on-change launch logs report the wait once rather than every tick.
RATE_LIMIT_DEFER_REASON = "github_rate_limited"


def rate_limited_launch_skips(
    snapshot: "OrchestratorSnapshot", hold: RateLimitEpisode
) -> list["SkippedItem"]:
    """Why each queued launch waits this tick, when the host's window is open.

    The planner's explanation of a rate-limited tick: every queued request is
    named with the reset it waits for, so "waiting for GitHub" is visible per
    item instead of reading as a launch that silently never happened.
    """
    from .planner_types import SkippedItem

    reason = (
        f"{RATE_LIMIT_DEFER_REASON}: GitHub {hold.limit.kind} rate limit"
        f" resets at {hold.limit.resets_at.isoformat()}"
    )
    items: list[tuple[str, int | None]] = [
        *(("review", review.pr_number) for review in snapshot.pending_reviews),
        *(
            ("retrospective_review", review.issue_number)
            for review in snapshot.pending_retrospective_reviews
        ),
        *(
            ("rework", rework.resolve_issue_number())
            for rework in snapshot.pending_reworks
        ),
        *(
            ("validation_retry", retry.issue_number)
            for retry in snapshot.pending_validation_retries
        ),
        *(("tech_lead", item.issue_number) for item in snapshot.pending_tech_lead),
    ]
    return [
        SkippedItem(item_type=item_type, number=number, reason=reason)
        for item_type, number in items
        if number is not None
    ]


def _past_bound(reason: str, episode: RateLimitEpisode) -> LaunchResult:
    """A rate-limit refusal past the deferral bound: counted as a failed launch."""
    return LaunchResult(
        None,
        False,
        f"{reason} (GitHub has rate limited launches for {_minutes(episode)} min "
        f"without a launch getting through, past the "
        f"{int(RATE_LIMIT_DEFERRAL_BOUND.total_seconds() // 60)} min deferral "
        "bound; counting this attempt as a failed launch)",
        disposition=LaunchDisposition.RETRYABLE_FAILURE,
    )


def _minutes(episode: RateLimitEpisode) -> int:
    return int(episode.limited_for.total_seconds() // 60)


__all__ = [
    "RATE_LIMIT_DEFER_REASON",
    "HostRateLimitLaunchGate",
    "LaunchMutations",
    "apply_launch_mutations",
    "converge_claim",
    "rate_limited_launch_skips",
]
