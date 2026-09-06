"""Repository observation fetching, deduplication and scoped candidate projection."""

from dataclasses import dataclass
import logging

from ..infra.config import Config
from ..events import EventName
from ..ports import EventSink, RepositoryHost, make_trace_event
from ..ports.issue import Issue
from .issue_refresh_batch import IssueRefreshBatch

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueObservationFetcher:
    """Keep raw host facts while applying configured candidate exclusions."""

    config: Config
    repository_host: RepositoryHost
    events: EventSink | None

    def fetch(
        self,
        labels_for_agent: list[str],
        milestone: str | None = None,
        required_stable_ids: set[str] | None = None,
        fetch_limit: int | None = None,
    ) -> IssueRefreshBatch:
        """Retain every fetched fact independently of candidate exclusions."""
        milestones = self.config.get_filter_milestones() or [milestone]
        limit = fetch_limit if fetch_limit is not None else self.config.filtering.fetch_limit
        all_issues, seen, still_needed = [], set(), set(required_stable_ids) if required_stable_ids else None

        for agent_label in self.config.agents.keys():
            labels = list(labels_for_agent) + [agent_label]
            for milestone_name in milestones:
                issues = self.repository_host.list_issues(
                    labels=labels, milestone=milestone_name,
                    limit=limit, required_stable_ids=still_needed,
                )
                self._process_fetched_issues(issues, all_issues, seen, still_needed, agent_label, labels, milestone_name)

        return IssueRefreshBatch(
            tuple(self._apply_issue_filter(all_issues)), tuple(all_issues), None,
        )

    def _process_fetched_issues(
        self,
        issues: list[Issue],
        all_issues: list[Issue],
        seen: set[int],
        still_needed: set[str] | None,
        agent_label: str,
        labels: list[str],
        milestone_name: str | None,
    ) -> None:
        """Process fetched issues and emit events."""
        for issue in issues:
            if issue.number in seen:
                continue
            seen.add(issue.number)
            all_issues.append(issue)
            if still_needed and issue.key.stable_id() in still_needed:
                still_needed.discard(issue.key.stable_id())

        if self.events is not None:
            self._emit_issues_fetched_events(issues, agent_label, labels, milestone_name)

    def _emit_issues_fetched_events(self, issues: list[Issue], agent_label: str, labels: list[str], milestone_name: str | None) -> None:
        """Emit events for fetched issues."""
        self.events.publish(make_trace_event(EventName.ISSUES_FETCHED, {
            "agent": agent_label, "labels": labels, "milestone": milestone_name,
            "count": len(issues), "issue_numbers": [i.number for i in issues],
        }))

    def _apply_issue_filter(self, all_issues: list[Issue]) -> list[Issue]:
        """Apply exclusion filter to issues."""
        issue_filter = self.config.get_issue_filter()
        if issue_filter.is_empty():
            return all_issues
        before_count = len(all_issues)
        filtered = issue_filter.apply(all_issues)
        if before_count != len(filtered):
            logger.debug("Excluded %d issues via filter %s", before_count - len(filtered), issue_filter)
        return filtered

