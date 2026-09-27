"""A GitHub write the orchestrator owes, and how it is paced (#7350).

The action liveness owner keeps every write it is obliged to land durably:
a park's needs-human block and its comment, the withdrawal of a block, and a
reconciliation pause observed drift calls for. Each is a debt: attempted, and
if GitHub refuses, retried from its durable row until it commits.

What a refusal costs depends on what GitHub said. A typed rate limit
(#7303's :class:`~.host_rate_limit.HostRateLimit`) says when the host answers
again: the debt waits until then and spends nothing, within the same
declared-wait bound an action gets. Any other refusal spends an attempt and
waits ``max_backoff``. The policy that applies these lives on
:class:`~.action_liveness.LivenessPolicy` (``effect_after`` / ``effect_due``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .host_rate_limit import HostRateLimit


@dataclass(frozen=True, slots=True)
class EffectResult:
    """How one attempt of an owed write ended."""

    committed: bool
    #: The host's typed rate limit behind a refusal, when it named one.
    rate_limit: HostRateLimit | None = None
    #: Why it did not commit; empty when it did.
    error: str = ""

    def __post_init__(self) -> None:
        if self.committed and (self.rate_limit is not None or self.error):
            raise ValueError("a committed write has no refusal")
        if not self.committed and not self.error.strip():
            raise ValueError("a refused write needs its reason")

    @classmethod
    def landed(cls) -> "EffectResult":
        return cls(True)

    @classmethod
    def refused(cls, error: str, rate_limit: HostRateLimit | None = None) -> "EffectResult":
        return cls(False, rate_limit, error)


@dataclass(frozen=True, slots=True)
class EffectDebt:
    """The durable pacing of one owed write that has not committed yet.

    ``attempts`` counts refusals that SPENT budget (a rate-limited refusal
    within the declared-wait bound does not). ``retry_at`` is when it may be
    tried again (``None``: now). ``first_failed_at`` starts the declared-wait
    bound, so a rate limit that never lifts still spends.
    """

    attempts: int = 0
    attempted_at: datetime | None = None
    retry_at: datetime | None = None
    first_failed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative")
        if (self.attempts == 0) != (self.attempted_at is None):
            raise ValueError("an attempt count needs its attempt time")
        for name in ("attempted_at", "retry_at", "first_failed_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        if self.retry_at is not None and self.first_failed_at is None:
            raise ValueError("a deferred write needs its first refusal time")


#: Nothing refused yet.
NO_DEBT = EffectDebt()


__all__ = ["NO_DEBT", "EffectDebt", "EffectResult"]
