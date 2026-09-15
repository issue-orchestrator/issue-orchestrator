"""Provider availability policy (shared owner for planner and launcher).

Owns every "is this issue affected by a provider outage, and what should the
orchestrator do about it" decision: which providers an issue depends on,
whether their circuits are open, and the typed provider-impact transition that
carries the blocked-label mutation *and* its durable issue-scoped record.

Call sites ask this owner for actions; they never assemble the label mutation
themselves, so the label and the history record cannot drift apart (#5980 F1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from typing import TYPE_CHECKING

from ..ports.issue import Issue
from ..domain.provider_lane import ProviderLane
from ..ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadiness,
    ProviderReadinessProbe,
)
from ..infra.config import Config
from .actions import Action
from .provider_impact import (
    ApplyProviderImpactAction,
    ProviderImpactAssessment,
    ProviderImpactTransition,
)
from .provider_resilience import ProviderResilienceManager
from .reconciliation import build_expected_for_mutation

if TYPE_CHECKING:
    from ..domain.models import AgentConfig
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot, PlanContext, SkippedItem

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProviderLaunchOutcome:
    """The one typed answer to "may I launch against this provider?" (#6999 A1).

    Carries both halves of the question so no consumer has to re-ask either:
    what the provider's own credential probe said, and whether the circuit
    owner is holding launches back. Planning and launch control consume the
    same value, which is why they cannot drift apart on eligibility.
    """

    provider: str
    readiness: ProviderReadiness
    circuit_open: bool
    #: The independently-metered lane this launch would draw from, as the
    #: circuit keys it. Equal to ``provider`` unless the agent's model bills
    #: against a separate meter (Fable, Codex-Spark), so every pre-lane
    #: configuration keeps its exact circuit row.
    lane: str = ""

    @property
    def lane_key(self) -> str:
        """The circuit key for this launch, falling back to the provider."""
        return self.lane or self.provider

    @property
    def may_launch(self) -> bool:
        """Whether a session may be spawned for this provider right now."""
        return self.readiness.launchable and not self.circuit_open

    @property
    def blocked_by_readiness(self) -> bool:
        """Whether the provider's own probe refused, whatever the reason.

        Covers an expired login and a provider that is not installed. Both are
        human-fixable in the sense that agent retries are pure waste, but only
        the former is an *auth* failure — the circuit owner is told about that
        one alone (#6999 F6).
        """
        return not self.readiness.launchable

    @classmethod
    def no_provider(cls) -> "ProviderLaunchOutcome":
        """The outcome for work that names no provider: nothing to gate on."""
        return cls(
            provider="",
            readiness=ProviderReadiness.unknown("", "no provider configured"),
            circuit_open=False,
            lane="",
        )


@dataclass(frozen=True)
class ProviderAvailabilityPolicy:
    config: Config
    provider_resilience: ProviderResilienceManager
    label_manager: "LabelManager | None" = None
    readiness_probe: ProviderReadinessProbe = NO_PROVIDER_READINESS_PROBE

    def blocked_label(self) -> str:
        if self.label_manager is not None:
            return self.label_manager.provider_unavailable
        # Deprecated fallback — callers should provide label_manager
        from .label_manager import LabelManager
        return LabelManager(self.config).provider_unavailable

    def provider_for_agent_label(self, agent_label: str | None) -> str | None:
        if not agent_label:
            return None
        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return None
        return agent_config.provider

    def provider_for_issue(self, issue: Issue) -> str | None:
        return self.provider_for_agent_label(issue.agent_type)

    def model_for_agent_label(self, agent_label: str | None) -> str | None:
        if not agent_label:
            return None
        agent_config = self.config.agents.get(agent_label)
        return agent_config.model if agent_config else None

    def lane_key_for_agent_label(self, agent_label: str | None) -> str | None:
        """The circuit key for an agent, which is its lane and not its provider.

        Returns ``None`` for an agent that names no provider, matching
        :meth:`provider_for_agent_label`, so callers gate on the same emptiness
        check they already had.
        """
        provider = self.provider_for_agent_label(agent_label)
        if not provider:
            return None
        return self.readiness_probe.lane_for(
            provider, self.model_for_agent_label(agent_label)
        ).key

    def lane_key_for_issue(self, issue: Issue) -> str | None:
        return self.lane_key_for_agent_label(issue.agent_type)

    def lanes_for_snapshot(self, snapshot: OrchestratorSnapshot) -> dict[int, set[str]]:
        """Map each issue to the quota lanes its work would draw from.

        Lane identity comes only from the snapshot's pre-planning sample. That
        keeps this planner-owned path free of credential subprocesses and makes
        the observe-before-plan ordering explicit in the input contract. Lanes
        rather than providers are used because that is what the circuit records
        and what :meth:`assess` reads.
        """
        lanes_by_issue: dict[int, set[str]] = {}

        for issue in snapshot.issues:
            lane = snapshot.provider_launch.lane_for_agent_label(issue.agent_type)
            if lane:
                lanes_by_issue.setdefault(issue.number, set()).add(lane)

        for review in snapshot.pending_reviews:
            reviewer_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
            lane = snapshot.provider_launch.lane_for_agent_label(reviewer_label)
            if lane:
                lanes_by_issue.setdefault(review.issue_number, set()).add(lane)

        for rework in snapshot.pending_reworks:
            issue_num = rework.resolve_issue_number()
            if issue_num is None:
                continue
            lane = snapshot.provider_launch.lane_for_agent_label(rework.agent_type)
            if lane:
                lanes_by_issue.setdefault(issue_num, set()).add(lane)

        tech_lead_lane = snapshot.provider_launch.lane_for_agent_label(
            self.config.tech_lead_review_agent
        )
        if self.config.tech_lead_enabled and tech_lead_lane:
            for tech_lead in snapshot.pending_tech_lead:
                lanes_by_issue.setdefault(tech_lead.issue_number, set()).add(tech_lead_lane)

        return lanes_by_issue

    def circuit_is_open(self, provider: str | None) -> bool:
        """Raw circuit read — "does the resilience owner still hold this?".

        Deliberately NOT a launch gate: it takes no readiness sample, so on its
        own it can never observe a human re-authenticating and would keep the
        fleet parked for the whole auth cooldown (#6999 F1). Launch paths call
        :meth:`assess_launch` instead (via the per-tick sampler). This remains for
        the readers that genuinely ask about circuit *ownership* rather than
        eligibility, such as the tech-lead stuck sweep.

        Also deliberately NOT used to decide what a provider-impact record says:
        anything that ends up in an issue's history goes through :meth:`assess`,
        so the record cannot describe a different instant or a wider set of
        providers than the decision that produced it (#5980 F4/A2).
        """
        if not provider:
            return False
        return self.provider_resilience.is_open(provider)

    def circuit_is_open_for_issue(self, issue: Issue) -> bool:
        """Whether any billing-compatible lane still owns ``issue``.

        This ownership read deliberately does not choose one billing mode. A
        launch can default unknown billing to metered, but a recovery sweep
        must not release an issue merely because the credential probe is now
        unavailable. The readiness boundary supplies the provider's static
        meter map without spawning a subprocess, and any matching open lane
        conservatively retains ownership.
        """
        if not issue.agent_type:
            return False
        agent = self.config.agents.get(issue.agent_type)
        if agent is None or not agent.provider:
            return False
        return any(
            self.circuit_is_open(lane.key)
            for lane in self.readiness_probe.candidate_lanes(
                agent.provider, agent.model
            )
        )

    # ------------------------------------------------------------------
    # Bounded provider launch assessment (#6999 A1)
    #
    # ONE answer to "may I launch against this provider right now?". Both
    # halves of the question — the credential sample and the circuit — are
    # resolved here, in that order, and the sample's circuit consequence is
    # recorded exactly once. Callers are the per-tick sampler (whose result
    # planning reads as a fact) and the launch-time gate. No call site reads
    # the raw circuit to make a launch decision, so an open auth circuit can
    # never suppress the very probe that would retire it.
    # ------------------------------------------------------------------

    def assess_launch(
        self,
        agent: "AgentConfig",
        *,
        now: datetime | None = None,
    ) -> ProviderLaunchOutcome:
        """Assess one configured agent, feed the circuit, and read the result.

        Order matters and is the whole fix: the probe runs *before* the circuit
        is consulted. While an auth circuit is open no session runs, so a gate
        that short-circuited on the open circuit could never observe the human
        re-authenticating (#6999 F1). The circuit is then read *after* the
        sample is recorded, so this one outcome reflects the sample it was
        derived from.

        Provider and model arrive on the same ``AgentConfig`` used to build the
        eventual command, so callers cannot accidentally gate one provider
        against a model copied from another agent. Both directions are reported to
        :class:`~.provider_resilience.ProviderResilienceManager` here rather
        than at the call sites, so the circuit is the only thing that decides
        how many failures are tolerated, how long launches stay paused, and when
        an outage is over. Control receives only the typed outcome — never a
        banner or exit code.
        """
        provider = agent.provider
        if not provider:
            return ProviderLaunchOutcome.no_provider()
        readiness = self.readiness_probe.check_launch_readiness(provider)
        # The lane is resolved from the same cached sample the readiness came
        # from, so the key this records under and the key it then reads are
        # necessarily the same one. Recording a Spark failure against `codex`
        # while the gate consults `codex:spark` would leave the lane permanently
        # unprotected — worse than the conflation lanes exist to fix.
        lane = self.lane_for_agent(agent).key
        if readiness.human_fixable:
            self.provider_resilience.record_auth_failure(
                lane,
                error_summary=readiness.detail or "provider is not authenticated",
                sample_id=readiness.sample_id,
                now=now,
            )
        elif readiness.authenticated:
            self.provider_resilience.clear_auth_failures(lane, now=now)
        return ProviderLaunchOutcome(
            provider=provider,
            readiness=readiness,
            circuit_open=self.provider_resilience.is_open(lane, now),
            lane=lane,
        )

    def lane_for_agent(self, agent: "AgentConfig") -> ProviderLane:
        """Resolve the lane and billing observed for this configured launch."""
        if not agent.provider:
            raise ValueError("cannot resolve a provider lane for an agent without a provider")
        return self.readiness_probe.lane_for(agent.provider, agent.model)

    def should_add_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label not in issue_labels and label not in planned_labels

    def should_remove_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label in issue_labels and label not in planned_labels

    # ------------------------------------------------------------------
    # Provider-impact transitions (#5980)
    #
    # The only supported way to move an issue's provider-blocked label. The
    # returned command carries the durable issue-scoped record with it, so a
    # caller cannot apply the label and forget the history.
    # ------------------------------------------------------------------

    def assess(
        self,
        providers: Iterable[str],
        *,
        now: datetime | None = None,
    ) -> ProviderImpactAssessment:
        """Read every named circuit once, at one instant.

        The single source of provider truth for a transition: the label
        decision (``assessment.blocked``), which providers are actually to
        blame, the retry window, and the history wording all come from this one
        read, so they cannot describe different moments or different providers
        (#5980 F4/A2). ``now`` is resolved once here and passed to every circuit
        read — never re-derived per provider.
        """
        assessed_at = now or _now()
        return ProviderImpactAssessment.from_statuses(
            assessed_at,
            (
                (provider, self.provider_resilience.status(provider, assessed_at))
                for provider in sorted(providers)
            ),
        )

    def blocked_transition(
        self,
        issue_number: int,
        assessment: ProviderImpactAssessment,
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        """Command for "this issue is blocked by an open provider circuit".

        Only the actually-open providers reach the record; the action rejects an
        assessment with no open circuit rather than recording an empty outage.
        """
        return ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.BLOCKED,
            label=self.blocked_label(),
            assessment=assessment,
            issue_key=issue_key,
            reason=f"provider unavailable: {', '.join(assessment.open_providers)}",
            expected=build_expected_for_mutation(),
        )

    def cleared_transition(
        self,
        issue_number: int,
        assessment: ProviderImpactAssessment,
        *,
        issue_key: str = "",
    ) -> ApplyProviderImpactAction:
        """Command for "no circuit is open; release this issue".

        The assessment decides whether this is a confirmed-healthy release or a
        mere cooldown expiry, so the history never claims recovery the circuit
        owner has not observed.
        """
        action = ApplyProviderImpactAction(
            issue_number=issue_number,
            transition=ProviderImpactTransition.CLEARED,
            label=self.blocked_label(),
            assessment=assessment,
            issue_key=issue_key,
            reason=(
                f"provider {assessment.release_kind.value}: "
                f"{', '.join(assessment.assessed_providers)}"
            ),
            expected=build_expected_for_mutation(),
        )
        return action

    # ------------------------------------------------------------------
    # Planning (moved out of Planner: provider policy has one owner)
    # ------------------------------------------------------------------

    def record_provider_skip(
        self,
        *,
        issue_number: int,
        item_type: str,
        item_number: int,
        provider: str,
        actions: list[Action],
        skipped: "list[SkippedItem]",
        plan_context: "PlanContext",
        now: datetime | None = None,
    ) -> None:
        """Record a launch skipped because ``provider``'s circuit is open."""
        from .planner_types import SkippedItem

        skipped.append(SkippedItem(
            item_type=item_type,
            number=item_number,
            reason=f"provider unavailable: {provider}",
        ))
        logger.info(
            "[issue #%s] Skipped: reason=provider_unavailable provider=%s",
            issue_number,
            provider,
        )
        issue_labels = plan_context.issue_labels(issue_number)
        planned_labels = plan_context.planned_adds(issue_number)
        if not self.should_add_blocked_label(issue_labels, planned_labels):
            return
        assessment = self.assess((provider,), now=now)
        if not assessment.blocked:
            # The cooldown elapsed between the skip decision and this read; the
            # item stays skipped for this tick, but blocking the issue (and
            # recording an outage) would no longer be true.
            return
        actions.append(self.blocked_transition(issue_number, assessment))
        plan_context.record_add(issue_number, self.blocked_label())

    def plan_provider_impact(
        self,
        snapshot: "OrchestratorSnapshot",
        plan_context: "PlanContext",
        now: datetime | None = None,
    ) -> list[Action]:
        """Plan provider-impact transitions for every in-scope issue.

        ``now`` fixes the instant every circuit in this pass is read at, so a
        single planning cycle cannot mix reads from different moments.
        """
        actions: list[Action] = []
        label = self.blocked_label()
        assessed_at = now or _now()
        lanes_by_issue = self.lanes_for_snapshot(snapshot)
        for issue in snapshot.issues:
            lanes = lanes_by_issue.get(issue.number, set())
            if not lanes:
                continue
            assessment = self.assess(lanes, now=assessed_at)
            issue_labels = plan_context.issue_labels(issue.number)
            planned_labels = plan_context.planned_adds(issue.number)
            issue_key = issue.key.stable_id()
            if assessment.blocked and self.should_add_blocked_label(
                issue_labels, planned_labels
            ):
                actions.append(
                    self.blocked_transition(issue.number, assessment, issue_key=issue_key)
                )
                plan_context.record_add(issue.number, label)
            if (
                not assessment.blocked
                and self.should_remove_blocked_label(issue_labels, planned_labels)
                and plan_context.should_remove_label(issue.number, label)
            ):
                actions.append(
                    self.cleared_transition(issue.number, assessment, issue_key=issue_key)
                )
                plan_context.record_remove(issue.number, label)
        return actions
