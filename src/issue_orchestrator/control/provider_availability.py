"""Provider availability policy helpers (shared between planner and launcher)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from typing import TYPE_CHECKING

from ..ports.issue import Issue
from ..infra.config import Config
from .provider_resilience import ProviderResilienceManager

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from .planner_types import OrchestratorSnapshot


@dataclass(frozen=True)
class ProviderAvailabilityPolicy:
    config: Config
    provider_resilience: ProviderResilienceManager
    label_manager: "LabelManager | None" = None

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

    def providers_for_snapshot(self, snapshot: OrchestratorSnapshot) -> dict[int, set[str]]:
        providers_by_issue: dict[int, set[str]] = {}

        for issue in snapshot.issues:
            provider = self.provider_for_issue(issue)
            if provider:
                providers_by_issue.setdefault(issue.number, set()).add(provider)

        for review in snapshot.pending_reviews:
            reviewer_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
            provider = self.provider_for_agent_label(reviewer_label)
            if provider:
                providers_by_issue.setdefault(review.issue_number, set()).add(provider)

        for rework in snapshot.pending_reworks:
            issue_num = rework.resolve_issue_number()
            if issue_num is None:
                continue
            provider = self.provider_for_agent_label(rework.agent_type)
            if provider:
                providers_by_issue.setdefault(issue_num, set()).add(provider)

        tech_lead_provider = self.provider_for_agent_label(self.config.tech_lead_review_agent)
        if tech_lead_provider:
            for tech_lead in snapshot.pending_tech_lead:
                providers_by_issue.setdefault(tech_lead.issue_number, set()).add(tech_lead_provider)

        return providers_by_issue

    def is_open(self, provider: str | None) -> bool:
        if not provider:
            return False
        return self.provider_resilience.is_open(provider)

    def any_open(self, providers: Iterable[str]) -> bool:
        return any(self.is_open(provider) for provider in providers)

    def should_add_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label not in issue_labels and label not in planned_labels

    def should_remove_blocked_label(self, issue_labels: Iterable[str], planned_labels: set[str]) -> bool:
        label = self.blocked_label()
        return label in issue_labels and label not in planned_labels
