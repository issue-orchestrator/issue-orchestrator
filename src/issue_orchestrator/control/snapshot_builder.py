"""Snapshot builder for control API and test tooling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..domain.issue_key import StableIssueId
from ..infra.config import Config
from ..infra import gh_audit
from ..ports.repository_host import RepositoryHost
from ..domain.models import OrchestratorState


@dataclass
class SnapshotBuilder:
    """Builds a lightweight snapshot for test synchronization."""

    config: Config
    repository_host: RepositoryHost

    def build_snapshot(
        self,
        state: OrchestratorState,
        snapshot_id: int,
        last_tick_id: int | None,
    ) -> dict[str, Any]:
        with gh_audit.context(
            reason=gh_audit.AuditReason.SNAPSHOT_REFRESH,
            scope=gh_audit.AuditScope.MANUAL,
        ):
            issues = self._fetch_issues()
        issue_views: dict[StableIssueId, Any] = {}

        for issue in issues:
            issue_key = issue.key.stable_id()
            pr_view = self._get_pr_view(issue.number)
            issue_views[issue_key] = {
                "labels": list(issue.labels),
                "state": issue.state,
                "pr": pr_view,
                "apply_attempts": 0,
                "reconcile_required": 0,
            }

        idle = (
            len(state.active_sessions) == 0
            and len(state.pending_reviews) == 0
            and len(state.pending_reworks) == 0
            and len(state.pending_tech_lead_reviews) == 0
        )

        return {
            "snapshot_id": snapshot_id,
            "orchestrator": {
                "idle": idle,
                "paused": state.paused,
                "last_tick_id": last_tick_id,
            },
            "issues": issue_views,
        }

    def _fetch_issues(self) -> list[Any]:
        labels_for_agent: list[str] = []
        if self.config.filtering.label:
            labels_for_agent.append(self.config.filtering.label)

        milestones = self.config.get_filter_milestones()
        if not milestones:
            milestones = [None]

        all_issues: list[Any] = []
        seen: set[int] = set()
        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent)
            labels.append(agent_label)
            for milestone in milestones:
                issues = self.repository_host.list_issues(
                    labels=labels,
                    milestone=milestone,
                    limit=self.config.filtering.fetch_limit,
                )
                for issue in issues:
                    if issue.number in seen:
                        continue
                    seen.add(issue.number)
                    all_issues.append(issue)

        # Apply exclusion filter (runs after GitHub API filtering)
        issue_filter = self.config.get_issue_filter()
        if not issue_filter.is_empty():
            all_issues = issue_filter.apply(all_issues)

        return all_issues

    def _get_pr_view(self, issue_number: int) -> dict[str, Any]:
        pr_view: dict[str, Any] = {
            "number": None,
            "draft": None,
            "labels": [],
        }
        try:
            prs = self.repository_host.get_prs_for_issue(issue_number, state="all")
        except Exception:
            return pr_view
        if not prs:
            return pr_view

        pr = _select_primary_pr(prs)
        pr_view.update({
            "number": pr.number,
            "draft": None,
            "labels": list(pr.labels),
        })
        return pr_view


def _select_primary_pr(prs: list[Any]) -> Any:
    for state in ("open", "merged", "closed"):
        for pr in prs:
            if pr.state == state:
                return pr
    return prs[0]
