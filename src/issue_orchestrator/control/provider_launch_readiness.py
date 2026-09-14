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
from dataclasses import dataclass, field
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
    lanes_by_agent_label: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ProviderLaunchReadiness":
        """The explicit "nothing was sampled this tick" fact.

        Blocks nothing, which is correct for the compositions that wire no
        sampler at all (tests, and any run without a circuit owner). It is a
        named absence rather than a silent default: a production tick always
        carries a real sample.
        """
        return cls(outcomes={}, lanes_by_agent_label={})

    def lane_for_agent_label(self, agent_label: str | None) -> str | None:
        """Return the sampled quota lane for ``agent_label``, if any.

        This mapping is part of the observed fact so planning never has to
        re-run the provider probe merely to recover the lane identity.
        """
        if not agent_label:
            return None
        return self.lanes_by_agent_label.get(agent_label)

    def outcome_for_agent_label(
        self, agent_label: str | None
    ) -> ProviderLaunchOutcome | None:
        """Return the sampled launch outcome for ``agent_label``, if any."""
        return self.outcome_for(self.lane_for_agent_label(agent_label))

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
    """Samples every configured provider/model target once per tick.

    One assessment per distinct configured target, not per queue item: the
    probe's own cache collapses targets on the same provider onto one physical
    credential check, and sampling here means the whole plan is decided against
    one consistent reading.
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
        outcomes_by_pair: dict[tuple[str, str], ProviderLaunchOutcome] = {}
        outcomes: dict[str, ProviderLaunchOutcome] = {}
        lanes_by_agent_label: dict[str, str] = {}
        for agent_label, agent in sorted(self.config.agents.items()):
            if not agent.provider:
                continue
            pair = (agent.provider, agent.model or "")
            outcome = outcomes_by_pair.get(pair)
            if outcome is None:
                outcome = self.policy.assess_launch(agent, now=now)
                outcomes_by_pair[pair] = outcome
            outcomes.setdefault(outcome.lane_key, outcome)
            lanes_by_agent_label[agent_label] = outcome.lane_key
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
        return ProviderLaunchReadiness(
            outcomes=outcomes,
            lanes_by_agent_label=lanes_by_agent_label,
        )


__all__ = ["ProviderLaunchReadiness", "ProviderLaunchReadinessSampler"]
