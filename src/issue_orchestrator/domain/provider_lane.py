"""Provider quota lanes: the unit capacity is actually exhausted in (#7253).

A *lane* is one independently-metered pool of provider capacity. It is not the
same thing as a provider:

* ``claude-code`` meters Fable separately from Opus/Sonnet/Haiku.
* ``codex`` meters ``gpt-5.3-codex-spark`` separately from ``gpt-6-astra``.
* ``deepseek`` is a separate vendor entirely.

Before this module the circuit breaker keyed on the provider *name*, so a Fable
quota trip closed the door on Opus and a Spark trip closed it on Astra. That
conflation was latent only because no configuration used the second meter;
shipping the ``fable`` and ``spark`` modes is what activates it.

The second fact this module carries is that **billing mode is a property of the
operator's account, not of the provider**. The same CLI is prepaid under
subscription auth and pay-as-you-go under API-key auth, and issue-orchestrator
is used by operators on both. Billing mode is therefore *observed* (at the
readiness probe, which already runs the auth command) and never assumed from the
provider name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["BillingMode", "ProviderLane"]


class BillingMode(str, Enum):
    """How a lane's capacity is paid for, which decides how exhaustion heals.

    The two modes are economic opposites, and conflating them is expensive in
    one direction only:

    ``PREPAID``
        Subscription quota. Free at the margin, and **use-it-or-lose-it** —
        unused capacity evaporates at the window reset. Exhaustion heals on a
        timer without anybody doing anything.

    ``METERED``
        Pay-as-you-go. Every token is incremental spend, and no window returns
        anything. Exhaustion means the balance is gone, and only a human
        topping it up clears it.

    When a probe cannot determine the mode, callers must default to
    ``METERED``. Treating prepaid capacity as metered merely under-uses a lane
    that was free; the reverse drains an operator's card.
    """

    PREPAID = "prepaid"
    METERED = "metered"

    @property
    def heals_on_timer(self) -> bool:
        """Whether waiting alone can restore this lane's exhausted capacity.

        This is the distinction the circuit breaker's quota deadline turns on.
        ``ProviderErrorType.QUOTA.requires_human_intervention`` is
        unconditionally true today, which is correct for a metered lane and
        needlessly pessimistic for a prepaid one: a weekly subscription meter
        refills on its own, so holding the circuit open until a human notices
        strands capacity the operator already paid for.
        """
        return self is BillingMode.PREPAID


@dataclass(frozen=True)
class ProviderLane:
    """One independently-metered pool of capacity, and how it is billed.

    ``provider`` plus the optional ``meter`` form the identity — the value the
    circuit breaker keys on. ``billing`` is carried alongside rather than being
    part of the identity: it governs how exhaustion *heals*, and it is re-read
    from the auth probe on every launch, so baking it into the key would orphan
    circuit state the moment an operator changed how they authenticate.

    ``meter`` is ``None`` whenever the provider exposes a single undivided
    pool. That is always the case under metered billing — API-key auth buys one
    pool of dollars, so there is no sub-meter to split and ``deepseek + flash``
    and ``deepseek + v4-pro`` are the same lane.
    """

    provider: str
    meter: str | None = None
    billing: BillingMode = BillingMode.METERED

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("ProviderLane requires a provider name")
        if self.meter is not None and not self.meter:
            raise ValueError(
                f"ProviderLane meter must be a name or None, got {self.meter!r}"
            )
        if self.meter is not None and self.billing is BillingMode.METERED:
            # A metered account buys one pool of currency. Splitting it into
            # sub-meters would invent independence that does not exist, and the
            # circuit would then let an exhausted balance keep launching work
            # on a "different" lane backed by the same empty balance.
            raise ValueError(
                f"metered billing has no sub-meters; {self.provider!r} cannot "
                f"declare meter {self.meter!r}"
            )

    @property
    def key(self) -> str:
        """The stable identity the circuit breaker persists.

        ``claude-code``, ``claude-code:fable``, ``codex``, ``codex:spark``,
        ``deepseek``. Chosen to stay readable in the database and in logs: the
        pre-lane rows were bare provider names, and a lane without a sub-meter
        keeps exactly that spelling so existing state keeps its meaning.
        """
        return self.provider if self.meter is None else f"{self.provider}:{self.meter}"

    @property
    def heals_on_timer(self) -> bool:
        """Whether exhausted capacity on this lane returns without a human."""
        return self.billing.heals_on_timer

    def __str__(self) -> str:
        return self.key
