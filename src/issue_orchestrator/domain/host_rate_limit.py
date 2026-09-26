"""The repository host's rate limit, and how long launches wait on it (#7297).

A rate limit belongs to the TOKEN, not to the work that tripped it: once GitHub
has said "not before 14:05", every launch whose preparation reads GitHub would
be refused the same way. So the window is one fact shared by every launch path,
opened by whichever launch observed the limit and read by the planner so no
launch is attempted - and no retry spent - until it has passed.

It is also bounded. A token that is rate limited forever (a runaway loop
elsewhere draining it, an installation whose quota is misconfigured) must still
reach a human rather than defer in silence, so the window remembers how long
the limit has held - until a launch gets through again. Past :data:`RATE_LIMIT_DEFERRAL_BOUND` a
rate-limited launch is treated as the ordinary retryable failure it would have
been before, and the queue's bounded budget and escalation take over.

In memory on purpose: a restart loses at most one wasted attempt, which
re-observes the limit and reopens the window at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

#: ``primary`` is the per-token budget (``x-ratelimit-remaining: 0``);
#: ``secondary`` is the host's abuse throttle (``retry-after``, or a body that
#: names a secondary rate limit).
HostRateLimitKind = Literal["primary", "secondary"]

#: How long a rate limit may hold, unbroken, before launches stop deferring on
#: it. GitHub's primary budget refills hourly, so two hours without a single
#: clear window is not a burst any more.
RATE_LIMIT_DEFERRAL_BOUND = timedelta(hours=2)



@dataclass(frozen=True)
class HostRateLimit:
    """The host refused because the token's rate limit is spent, until ``resets_at``.

    A rate limit is not a failure of the work that asked: the host says when it
    will answer again, and asking before then only burns attempts.
    """

    #: Timezone-aware instant the host will accept requests again.
    resets_at: datetime
    kind: HostRateLimitKind
    #: The budget that ran out (``core``, ``search``, ``graphql``), when the
    #: host named it.
    resource: str | None = None

    def __post_init__(self) -> None:
        if self.resets_at.tzinfo is None:
            raise ValueError("HostRateLimit.resets_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RateLimitEpisode:
    """One observation of the window, as the launch that observed it saw it."""

    #: The limit now governing the window: the latest reset seen this episode.
    limit: HostRateLimit
    limited_since: datetime
    observed_at: datetime

    @property
    def limited_for(self) -> timedelta:
        return self.observed_at - self.limited_since

    @property
    def bound_exceeded(self) -> bool:
        return self.limited_for >= RATE_LIMIT_DEFERRAL_BOUND


@dataclass(slots=True)
class HostRateLimitWindow:
    """The host's current rate-limit window, shared by every launch path.

    The HOLD is shared: one token, one reset, so no launch is attempted before
    it. The EPISODE - how long the limit has held, measured against the bound -
    is kept per launch path (``work``). Different paths spend different GitHub
    budgets: a review that gets through on ``core`` proves nothing about the
    ``search`` budget tech-lead prep needs. So only the refused path getting
    through again ends its episode.
    """

    _limit: HostRateLimit | None = None
    _refused_since: dict[str, datetime] = field(default_factory=dict)

    def open_at(self, now: datetime, work: str | None = None) -> RateLimitEpisode | None:
        """The episode still holding launches back at ``now``, if any.

        ``work`` names the launch path whose episode is measured; ``None``
        (the planner's whole-tick view) measures the oldest one, so a tick is
        past the bound as soon as any path is.
        """
        limit = self._limit
        if limit is None or now >= limit.resets_at:
            return None
        since = (
            min(self._refused_since.values(), default=now)
            if work is None
            else self._refused_since.get(work, now)
        )
        return RateLimitEpisode(limit=limit, limited_since=since, observed_at=now)

    def observe(self, limit: HostRateLimit, now: datetime, work: str) -> RateLimitEpisode:
        """Record that the host refused a ``work`` launch under ``limit``.

        Extends that path's episode however long ago the window closed: only
        positive evidence of recovery (:meth:`recovered`) ends it. Elapsed time
        alone proves nothing - a tick that happens to arrive late would
        otherwise restart the clock and keep the bound out of reach.
        """
        previous = self._limit
        governing = (
            limit
            if previous is None or limit.resets_at >= previous.resets_at
            else previous
        )
        self._limit = governing
        since = self._refused_since.setdefault(work, now)
        return RateLimitEpisode(limit=governing, limited_since=since, observed_at=now)

    def recovered(self, work: str) -> None:
        """A ``work`` launch got through: that path's episode is over."""
        self._refused_since.pop(work, None)
        if not self._refused_since:
            self._limit = None


__all__ = [
    "RATE_LIMIT_DEFERRAL_BOUND",
    "HostRateLimit",
    "HostRateLimitKind",
    "HostRateLimitWindow",
    "RateLimitEpisode",
]
