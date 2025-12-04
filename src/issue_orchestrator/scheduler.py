"""Issue scheduling and dependency analysis module."""

import re
from dataclasses import dataclass
from typing import Optional

from .models import Issue, AgentConfig
from .config import Config


@dataclass
class SchedulerResult:
    """Result of scheduling decision."""

    issues_to_launch: list[Issue]
    blocked_issues: list[tuple[Issue, str]]  # issue and reason


class Scheduler:
    """Handles issue scheduling, prioritization, and dependency analysis."""

    def __init__(self, config: Config) -> None:
        """Initialize scheduler with configuration.

        Args:
            config: Configuration object containing max_sessions and other settings.
        """
        self.config = config

    def get_available_issues(self, all_issues: list[Issue]) -> list[Issue]:
        """Filter to issues that can be worked on (not blocked, not in-progress).

        Args:
            all_issues: List of all issues to filter.

        Returns:
            List of issues that are not blocked or in-progress.
        """
        available = []
        for issue in all_issues:
            # Skip issues that are in-progress
            if issue.state == "closed":
                continue
            if any(label.name == "in-progress" for label in issue.labels):
                continue
            available.append(issue)
        return available

    def sort_by_priority(self, issues: list[Issue]) -> list[Issue]:
        """Sort issues by milestone, then priority label, then number.

        Args:
            issues: List of issues to sort.

        Returns:
            Sorted list of issues.
        """
        milestone_order = self._get_milestone_order()

        def sort_key(issue: Issue) -> tuple:
            # Extract milestone number if present
            milestone_num = float("inf")
            if issue.milestone:
                match = re.search(r"M(\d+)", issue.milestone.title)
                if match:
                    milestone_num = int(match.group(1))

            # Extract priority from labels
            priority_value = self._get_priority_value(issue)

            return (milestone_num, priority_value, issue.number)

        return sorted(issues, key=sort_key)

    def pick_next_batch(
        self,
        available: list[Issue],
        current_count: int,
        priority_overrides: list[int] = None,
    ) -> list[Issue]:
        """Pick up to (max_sessions - current_count) issues to launch.

        Args:
            available: List of available issues to choose from.
            current_count: Current number of active sessions/issues.
            priority_overrides: Optional list of issue numbers to prioritize.

        Returns:
            List of issues to launch.
        """
        if priority_overrides is None:
            priority_overrides = []

        # Calculate how many more issues we can launch
        remaining_slots = self.config.max_sessions - current_count
        if remaining_slots <= 0:
            return []

        picked = []

        # First, add priority overrides
        override_map = {issue.number: issue for issue in available}
        for issue_num in priority_overrides:
            if issue_num in override_map and len(picked) < remaining_slots:
                picked.append(override_map[issue_num])

        # Then add from sorted available issues
        sorted_issues = self.sort_by_priority(available)
        for issue in sorted_issues:
            if len(picked) >= remaining_slots:
                break
            if issue not in picked:
                picked.append(issue)

        return picked

    def analyze_dependencies(self, issues: list[Issue]) -> dict[int, list[int]]:
        """Analyze issue bodies for dependency mentions.

        Returns dict mapping issue number to list of blocking issue numbers.

        Looks for patterns like:
        - "blocked by #123"
        - "depends on #123"
        - "after #123"

        Args:
            issues: List of issues to analyze.

        Returns:
            Dictionary mapping issue number to list of blocking issue numbers.
        """
        dependencies: dict[int, list[int]] = {}

        # Patterns to match dependency mentions
        patterns = [
            r"blocked by #(\d+)",
            r"depends on #(\d+)",
            r"after #(\d+)",
            r"waiting for #(\d+)",
            r"requires #(\d+)",
        ]

        for issue in issues:
            blocking_issues = set()
            body = issue.body or ""

            # Search for all patterns
            for pattern in patterns:
                matches = re.finditer(pattern, body, re.IGNORECASE)
                for match in matches:
                    blocking_issues.add(int(match.group(1)))

            if blocking_issues:
                dependencies[issue.number] = sorted(list(blocking_issues))

        return dependencies

    def _get_priority_value(self, issue: Issue) -> int:
        """Get numeric priority value from issue labels.

        Args:
            issue: Issue to extract priority from.

        Returns:
            Priority value (0=high, 1=medium, 2=low, 3=none).
        """
        for label in issue.labels:
            if label.name == "priority:high":
                return 0
            if label.name == "priority:medium":
                return 1
            if label.name == "priority:low":
                return 2
        return 3

    def _get_milestone_order(self) -> dict[str, int]:
        """Get ordering for milestones.

        Returns:
            Dictionary mapping milestone name to order value.
        """
        return {
            "M6": 0,
            "M7": 1,
            "M8": 2,
            "M9": 3,
            "M10": 4,
            "M11": 5,
        }
