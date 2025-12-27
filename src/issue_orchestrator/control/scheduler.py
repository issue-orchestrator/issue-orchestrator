"""Issue scheduling and dependency analysis module.

Part of the control plane - makes scheduling decisions about which issues
to work on next based on priorities, dependencies, and capacity.
"""

import importlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, Sequence

from ..ports.issue import Issue

# Sort keys can contain floats (timestamps/inf), ints (numbers), or strings (names)
SortKey = tuple[float | int | str, ...]
from ..config import Config
from .. import labels
from .dependency_evaluator import DependencyEvaluator

logger = logging.getLogger(__name__)


# Built-in strategy aliases map to full module paths
# Users can use these short names or provide their own "module.path.ClassName"
BUILTIN_STRATEGIES = {
    "due_date": "issue_orchestrator.control.scheduler.DueDateStrategy",
    "number": "issue_orchestrator.control.scheduler.NumberStrategy",
    "pattern": "issue_orchestrator.control.scheduler.PatternStrategy",
    "name": "issue_orchestrator.control.scheduler.NameStrategy",
}


class MilestoneSortStrategy(Protocol):
    """Protocol for milestone sorting strategies."""

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get the sort key for an issue based on milestone.

        Returns a tuple where the first element is the milestone sort key,
        and remaining elements can be used for tie-breaking.
        """
        ...


class DueDateStrategy:
    """Sort by milestone due date (ascending), nulls last."""

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get sort key based on milestone due date."""
        if issue.milestone_due_on:
            try:
                # Parse ISO date string to datetime for proper sorting
                due_date = datetime.fromisoformat(issue.milestone_due_on.replace("Z", "+00:00"))
                return (due_date.timestamp(),)
            except (ValueError, AttributeError):
                pass
        # No due date or parse error - sort to end
        return (float("inf"),)


class NumberStrategy:
    """Sort by milestone number (ascending), nulls last."""

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get sort key based on milestone number."""
        if issue.milestone_number is not None:
            return (issue.milestone_number,)
        # No milestone number - sort to end
        return (float("inf"),)


class PatternStrategy:
    """Extract number from milestone name using regex pattern."""

    def __init__(self, pattern: str):
        """Initialize with regex pattern.

        Args:
            pattern: Regex pattern with one capture group for the number.
        """
        self.pattern = re.compile(pattern)

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get sort key by extracting number from milestone name."""
        if issue.milestone:
            match = self.pattern.search(issue.milestone)
            if match:
                try:
                    return (int(match.group(1)),)
                except (ValueError, IndexError):
                    pass
        # No milestone or pattern didn't match - sort to end
        return (float("inf"),)


class NameStrategy:
    """Sort alphabetically by milestone name, nulls last."""

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get sort key based on milestone name."""
        if issue.milestone:
            return (issue.milestone,)
        # No milestone - sort to end using a high unicode value
        return ("\uffff",)


def load_strategy_class(class_path: str) -> type:
    """Dynamically load a strategy class from a module path.

    Args:
        class_path: Full module path like "mymodule.MyStrategy" or
                   "issue_orchestrator.scheduler.DueDateStrategy"

    Returns:
        The strategy class (not an instance).

    Raises:
        ValueError: If the module or class cannot be found.
    """
    try:
        module_path, class_name = class_path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except (ValueError, ModuleNotFoundError, AttributeError) as e:
        raise ValueError(f"Cannot load strategy class '{class_path}': {e}") from e


def get_milestone_strategy(config: Config) -> MilestoneSortStrategy:
    """Factory function to get the appropriate milestone sort strategy.

    Uses dynamic import for ALL strategies, including built-ins.
    This ensures users can provide custom strategies using the same
    mechanism we use internally.

    Args:
        config: Configuration object containing milestone_sort setting.

    Returns:
        An instance of the appropriate strategy.

    Raises:
        ValueError: If the strategy cannot be loaded.
    """
    strategy_spec = config.milestone_sort.lower()

    # Resolve built-in aliases to full module paths
    if strategy_spec in BUILTIN_STRATEGIES:
        class_path = BUILTIN_STRATEGIES[strategy_spec]
    else:
        # Assume it's a full module path (user-provided plugin)
        class_path = config.milestone_sort

    # Dynamically load the strategy class
    strategy_class = load_strategy_class(class_path)

    # Instantiate with config kwargs (uniform for built-ins and plugins)
    # Strict mode: no **kwargs in constructors means unknown args raise TypeError
    return strategy_class(**config.milestone_sort_config)


@dataclass
class SchedulerResult:
    """Result of scheduling decision."""

    issues_to_launch: list[Issue]
    blocked_issues: list[tuple[Issue, str]]  # issue and reason
    dependency_blocked: list[tuple[Issue, str]] = field(default_factory=list)  # issues blocked by unsatisfied dependencies


class Scheduler:
    """Handles issue scheduling, prioritization, and dependency analysis."""

    def __init__(
        self,
        config: Config,
        milestone_strategy: Optional[MilestoneSortStrategy] = None,
        dependency_evaluator: Optional[DependencyEvaluator] = None,
    ) -> None:
        """Initialize scheduler with configuration.

        Args:
            config: Configuration object containing max_sessions and other settings.
            milestone_strategy: Optional milestone sorting strategy. If None, uses config to create one.
            dependency_evaluator: Optional evaluator to gate issues on dependencies.
        """
        self.config = config
        self.milestone_strategy = milestone_strategy or get_milestone_strategy(config)
        self.dependency_evaluator = dependency_evaluator

    def get_available_issues(
        self,
        all_issues: Sequence[Issue],
        check_dependencies: bool = True,
    ) -> tuple[list[Issue], list[tuple[Issue, str]]]:
        """Filter to issues that can be worked on (not blocked, not in-progress).

        Args:
            all_issues: List of all issues to filter.
            check_dependencies: Whether to check issue dependencies.

        Returns:
            Tuple of (available_issues, dependency_blocked) where:
            - available_issues: List of runnable issues
            - dependency_blocked: List of (issue, reason) for dependency-blocked issues
        """
        available = []
        dependency_blocked: list[tuple[Issue, str]] = []

        for issue in all_issues:
            if issue.state == "closed":
                continue
            if labels.is_in_progress(issue.labels):
                continue
            if labels.is_pr_pending(issue.labels):
                continue
            if labels.is_blocking_any(issue.labels):
                continue

            # Check dependencies if evaluator is available
            if check_dependencies and self.dependency_evaluator and issue.body:
                report = self.dependency_evaluator.evaluate(
                    issue_number=issue.number,
                    issue_body=issue.body,
                )
                if not report.runnable:
                    dependency_blocked.append((issue, report.summary()))
                    logger.debug(
                        "Issue #%d blocked by dependencies: %s",
                        issue.number,
                        report.summary(),
                    )
                    continue

            available.append(issue)

        return available, dependency_blocked

    def sort_by_priority(self, issues: Sequence[Issue]) -> list[Issue]:
        """Sort issues by milestone, priority tier, sequence, then issue number.

        Sort order (from naming standard):
        1. Milestone order: M0, M1, M2...
        2. Priority tier: P0 < P1 < P2 < P3
        3. Sequence: numeric part after dash in [Px-nnn]
        4. Tie-breaker: GitHub issue number ascending

        Args:
            issues: List of issues to sort.

        Returns:
            Sorted list of issues.
        """
        def sort_key(issue: Issue) -> SortKey:
            # Get milestone sort key from strategy
            milestone_key = self.milestone_strategy.get_sort_key(issue)

            # Extract priority tier and sequence from title [Px-nnn]
            priority_value = self._get_priority_value(issue)
            sequence_value = self._get_sequence_value(issue)

            # Combine: milestone, priority tier, sequence, issue number
            return milestone_key + (priority_value, sequence_value, issue.number)

        return sorted(issues, key=sort_key)

    def _get_sequence_value(self, issue: Issue) -> int | float:
        """Get sequence number from issue title [Px-nnn] pattern.

        Args:
            issue: Issue to extract sequence from.

        Returns:
            Sequence number, or infinity if not found (sorts last).
        """
        # Match [Px-nnn] pattern and extract nnn
        match = re.search(r"\[P\d-(\d+)\]", issue.title)
        if match:
            return int(match.group(1))
        return float("inf")  # No sequence = sort last

    def pick_next_batch(
        self,
        available: list[Issue],
        current_count: int,
        priority_overrides: Optional[list[int]] = None,
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
        remaining_slots = self.config.max_concurrent_sessions - current_count
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
        """Get numeric priority value from issue title [Px-nnn] pattern.

        Sort order: P0 < P1 < P2 < P3 (lower value = higher priority)

        Args:
            issue: Issue to extract priority from.

        Returns:
            Priority tier (0-3), or 9 if no priority in title.
        """
        match = re.search(r"\[P(\d)-\d+\]", issue.title)
        if match:
            return int(match.group(1))
        return 9  # No priority = sort last
