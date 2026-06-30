"""Issue scheduling and dependency analysis module.

Part of the control plane - makes scheduling decisions about which issues
to work on next based on priorities, dependencies, and capacity.
"""

import importlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Protocol, Sequence

from ..ports.issue import Issue
from ..domain.models import Session

# Sort keys can contain floats (timestamps/inf), ints (numbers), or strings (names)
SortKey = tuple[float | int | str, ...]
from ..infra.config import Config
from .dependency_evaluator import DependencyEvaluator

if TYPE_CHECKING:
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)


# Built-in strategy aliases map to full module paths
# Users can use these short names or provide their own "module.path.ClassName"
BUILTIN_STRATEGIES = {
    "due_date": "issue_orchestrator.control.scheduler.DueDateStrategy",
    "milestone_number": "issue_orchestrator.control.scheduler.MilestoneNumberStrategy",
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


class MilestoneNumberStrategy:
    """Extract first number from milestone name for sorting.

    Handles common patterns like M1, M10, Sprint 5, v2.0, etc.
    Sorts numerically so M1 < M2 < M10 (not alphabetically where M10 < M2).
    """

    def get_sort_key(self, issue: Issue) -> SortKey:
        """Get sort key by extracting first number from milestone name."""
        if issue.milestone:
            match = re.search(r"(\d+)", issue.milestone)
            if match:
                return (int(match.group(1)),)
        # No milestone or no number found - sort to end
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


@dataclass(frozen=True)
class IssueAvailabilityDecision:
    """Availability result for a single issue."""

    issue: Issue
    available: bool
    reason: str
    detail: str | None = None


class Scheduler:
    """Handles issue scheduling, prioritization, and dependency analysis."""

    def __init__(
        self,
        config: Config,
        milestone_strategy: Optional[MilestoneSortStrategy] = None,
        dependency_evaluator: Optional[DependencyEvaluator] = None,
        label_manager: "LabelManager | None" = None,
    ) -> None:
        """Initialize scheduler with configuration.

        Args:
            config: Configuration object containing max_sessions and other settings.
            milestone_strategy: Optional milestone sorting strategy. If None, uses config to create one.
            dependency_evaluator: Optional evaluator to gate issues on dependencies.
            label_manager: Label registry for prefix-aware queries.
        """
        self.config = config
        self.milestone_strategy = milestone_strategy or get_milestone_strategy(config)
        self.milestone_order = [m for m in config.milestone_order if m]
        self.milestone_order_map = {
            milestone: index for index, milestone in enumerate(self.milestone_order)
        }
        self.dependency_evaluator = dependency_evaluator
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def get_available_issues(
        self,
        all_issues: Sequence[Issue],
        check_dependencies: bool = True,
        active_sessions: Optional[Sequence[Session]] = None,
    ) -> tuple[list[Issue], list[tuple[Issue, str]]]:
        """Filter to issues that can be worked on (not blocked, not in-progress).

        Uses runtime-aware gating: an issue is only considered "in progress" if
        it has the in-progress label AND there's an active session working on it.
        If the label exists but no session is running, the issue is considered
        stale (will be cleaned up by planner) and NOT blocked.

        Args:
            all_issues: List of all issues to filter.
            check_dependencies: Whether to check issue dependencies.
            active_sessions: Currently active sessions (for runtime-aware gating).

        Returns:
            Tuple of (available_issues, dependency_blocked) where:
            - available_issues: List of runnable issues
            - dependency_blocked: List of (issue, reason) for dependency-blocked issues
        """
        available: list[Issue] = []
        dependency_blocked: list[tuple[Issue, str]] = []
        decisions = self.evaluate_issues(
            all_issues,
            check_dependencies=check_dependencies,
            active_sessions=active_sessions,
        )
        for decision in decisions:
            if decision.available:
                available.append(decision.issue)
                continue
            if decision.reason == "dependency_blocked":
                dependency_blocked.append((decision.issue, decision.detail or "dependency blocked"))
                logger.debug(
                    "Issue #%d blocked by dependencies: %s",
                    decision.issue.number,
                    decision.detail or "dependency blocked",
                )

        return available, dependency_blocked

    def evaluate_issues(
        self,
        all_issues: Sequence[Issue],
        check_dependencies: bool = True,
        active_sessions: Optional[Sequence[Session]] = None,
    ) -> list[IssueAvailabilityDecision]:
        """Evaluate scheduler availability for each issue with explicit reason codes."""
        active_issue_numbers = {s.issue.number for s in active_sessions} if active_sessions else set()
        return [
            self._evaluate_issue(
                issue,
                active_issue_numbers=active_issue_numbers,
                check_dependencies=check_dependencies,
            )
            for issue in all_issues
        ]

    def _evaluate_issue(
        self,
        issue: Issue,
        *,
        active_issue_numbers: set[int],
        check_dependencies: bool,
    ) -> IssueAvailabilityDecision:
        if issue.state == "closed":
            return IssueAvailabilityDecision(issue=issue, available=False, reason="closed")

        # Runtime-aware in-progress gating:
        # Only block if label exists AND session is actually running.
        if self._lm.is_in_progress(issue.labels) and issue.number in active_issue_numbers:
            return IssueAvailabilityDecision(issue=issue, available=False, reason="in_progress_active_session")

        # Label exists but no session - stale; planner handles cleanup, so still eligible.
        if self._lm.is_pr_pending(issue.labels):
            return IssueAvailabilityDecision(issue=issue, available=False, reason="pr_pending")
        if self._lm.is_blocking_any(issue.labels):
            return IssueAvailabilityDecision(issue=issue, available=False, reason="blocked_label")

        if check_dependencies and self.dependency_evaluator and issue.body:
            report = self.dependency_evaluator.evaluate_work_gate(
                issue_number=issue.number,
                issue_body=issue.body,
                source_milestone=issue.milestone,
            )
            if not report.can_start_work:
                return IssueAvailabilityDecision(
                    issue=issue,
                    available=False,
                    reason="dependency_blocked",
                    detail=report.work_summary(),
                )

        return IssueAvailabilityDecision(issue=issue, available=True, reason="available")

    def sort_by_priority(self, issues: Sequence[Issue]) -> list[Issue]:
        """Sort issues by milestone, priority tier, sequence, then issue number.

        Sort order (from naming standard):
        1. Milestone order: M0, M1, M2...
        2. Priority tier: P0 < P1 < P2 < ... < P9
        3. Sequence: numeric part after dash in [Px-nnn]
        4. Tie-breaker: GitHub issue number ascending

        Args:
            issues: List of issues to sort.

        Returns:
            Sorted list of issues.
        """
        def sort_key(issue: Issue) -> SortKey:
            # Get milestone sort key from strategy
            if self.milestone_order_map and issue.milestone in self.milestone_order_map:
                milestone_key = (0, self.milestone_order_map[issue.milestone])
            else:
                milestone_key = (1,) + self.milestone_strategy.get_sort_key(issue)

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

        Sort order: P0 < P1 < P2 < ... < P9 (lower value = higher priority)

        Args:
            issue: Issue to extract priority from.

        Returns:
            Priority tier (0-9). If no [P?-nnn] prefix, defaults to configured tier.
        """
        match = re.search(r"\[P(\d)-\d+\]", issue.title)
        if match:
            return int(match.group(1))
        return self.config.scheduling.default_priority_tier
