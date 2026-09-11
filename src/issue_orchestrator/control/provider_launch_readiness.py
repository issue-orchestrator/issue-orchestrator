"""Per-tick provider launch eligibility, sampled once and shared (#6999 A3).

Deciding "may I launch against this provider?" costs a credential probe and
feeds the circuit owner — external I/O and a shared-state write. Neither
belongs inside :class:`~.planner.Planner`, which is a pure function of its
snapshot: putting them there made planning depend on installed CLI/login state
and let a queue filter mutate the circuit as a side effect of deciding.

So the tick samples first, through :class:`ProviderLaunchReadinessSampler`, and
carries the result into the snapshot as a fact. Planning then reads
:class:`ProviderLaunchReadiness` — a plain lookup — and every queue sees the
same answer taken at the same instant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from ..infra.config import Config
from .provider_availability import ProviderAvailabilityPolicy, ProviderLaunchOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderLaunchReadiness:
    """One tick's answer for every quota lane the fleet can launch against.

    A *fact*, not a decision: it says which lanes are eligible right now and
    why, and it is read the same way by every queue.

    Keyed by lane rather than provider so that an exhausted Fable or Spark meter
    parks only the agents that draw on it. For an agent whose model has no
    separate meter the lane key is the provider name, so this is unchanged for
    every configuration that predates lanes.
    """

    outcomes: Mapping[str, ProviderLaunchOutcome]

    @classmethod
    def empty(cls) -> "ProviderLaunchReadiness":
        """The explicit "nothing was sampled this tick" fact.

        Blocks nothing, which is correct for the compositions that wire no
        sampler at all (tests, and any run without a circuit owner). It is a
        named absence rather than a silent default: a production tick always
        carries a real sample.
        """
        return cls(outcomes={})

    def outcome_for(self, lane: str | None) -> ProviderLaunchOutcome | None:
        """The sampled outcome for ``lane``, or ``None`` if unsampled."""
        if not lane:
            return None
        return self.outcomes.get(lane)

    def blocks(self, lane: str | None) -> bool:
        """Whether planning must not queue work for ``lane`` this tick.

        The *circuit* decides, not the raw readiness. That is deliberate: a
        readiness refusal that has not opened the circuit — a sub-threshold auth
        sample, or a provider that is simply not installed — has no issue-scoped
        consequence available at planning time, because the provider-impact
        command only records a transition when a circuit is actually open.
        Suppressing planning on it would drop the work with nothing to show for
        it on any issue (#6999 F6).

        So planning defers those to the launch gate, which refuses the launch
        per issue and says why. Once the circuit opens, planning parks the work
        up front and the impact command records it. Every non-launchable state
        therefore has exactly one owner and one issue-scoped outcome.
        """
        outcome = self.outcome_for(lane)
        return outcome is not None and outcome.circuit_open


@dataclass(frozen=True)
class ProviderLaunchReadinessSampler:
    """Samples every configured provider once per tick.

    One sample per provider per tick, not per queue item: the probe's own cache
    would collapse the repeats anyway, but sampling here also means the whole
    plan is decided against one consistent reading.
    """

    config: Config
    policy: ProviderAvailabilityPolicy

    def sample(self, now: datetime | None = None) -> ProviderLaunchReadiness:
        """Assess every quota lane any configured agent could launch against.

        Iterates ``(provider, model)`` pairs rather than bare providers: two
        agents on the same provider draw on different meters when one of them
        runs a separately-metered model, and sampling only the provider would
        collapse them back into the single circuit row lanes exist to split.
        Pairs that resolve to the same lane are sampled once.
        """
        pairs = sorted(
            {
                (agent.provider, agent.model or "")
                for agent in self.config.agents.values()
                if agent.provider
            }
        )
        outcomes: dict[str, ProviderLaunchOutcome] = {}
        for provider, model in pairs:
            outcome = self.policy.assess_launch(provider, model=model, now=now)
            outcomes.setdefault(outcome.lane_key, outcome)
        for lane, outcome in outcomes.items():
            if not outcome.may_launch:
                logger.info(
                    "[PROVIDER] lane %s is not launchable this tick: readiness=%s "
                    "circuit_open=%s (planning %s)",
                    lane,
                    outcome.readiness.state.value,
                    outcome.circuit_open,
                    "parks the work" if outcome.circuit_open else "defers to the launch gate",
                )
        return ProviderLaunchReadiness(outcomes=outcomes)


__all__ = ["ProviderLaunchReadiness", "ProviderLaunchReadinessSampler"]
