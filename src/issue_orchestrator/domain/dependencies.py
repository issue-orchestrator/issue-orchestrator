"""Dependency evaluation for issues.

This module implements deterministic dependency gating with no configuration options.

Rules (from design docs):
- Satisfied: dependency issue exists and is CLOSED
- Unsatisfied: dependency exists and is NOT closed
- Missing: dependency cannot be found (wrong number, wrong repo, permissions, deleted)
- Unknown: state cannot be determined due to transient error (rate limit/network)

An issue is runnable IFF all dependencies are satisfied.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence

logger = logging.getLogger(__name__)


class DependencyState(Enum):
    """State of a single dependency."""

    SATISFIED = "satisfied"  # Dependency issue is CLOSED
    UNSATISFIED = "unsatisfied"  # Dependency issue is OPEN
    MISSING = "missing"  # Cannot find dependency (404, permissions, deleted)
    UNKNOWN = "unknown"  # Transient error (network, rate limit)


@dataclass(frozen=True)
class Dependency:
    """A single dependency reference."""

    # The issue number being depended on
    issue_number: int

    # Optional: repository in owner/repo format (for cross-repo deps)
    repository: str | None = None

    # Resolved state
    state: DependencyState = DependencyState.UNKNOWN

    # Error message if missing/unknown
    error: str | None = None

    @property
    def is_satisfied(self) -> bool:
        return self.state == DependencyState.SATISFIED

    @property
    def blocks_running(self) -> bool:
        """Check if this dependency blocks the issue from running."""
        return self.state != DependencyState.SATISFIED


@dataclass(frozen=True)
class DependencyReport:
    """Complete dependency evaluation for an issue.

    Contains all dependencies grouped by state, plus the final runnable decision.
    """

    # Issue being evaluated
    issue_number: int

    # Dependencies by state
    satisfied: tuple[Dependency, ...] = field(default_factory=tuple)
    unsatisfied: tuple[Dependency, ...] = field(default_factory=tuple)
    missing: tuple[Dependency, ...] = field(default_factory=tuple)
    unknown: tuple[Dependency, ...] = field(default_factory=tuple)

    @property
    def runnable(self) -> bool:
        """Issue is runnable only if ALL dependencies are satisfied."""
        return (
            len(self.unsatisfied) == 0
            and len(self.missing) == 0
            and len(self.unknown) == 0
        )

    @property
    def all_dependencies(self) -> tuple[Dependency, ...]:
        """All dependencies regardless of state."""
        return self.satisfied + self.unsatisfied + self.missing + self.unknown

    @property
    def blocking_dependencies(self) -> tuple[Dependency, ...]:
        """Dependencies that block running."""
        return self.unsatisfied + self.missing + self.unknown

    @property
    def has_warnings(self) -> bool:
        """Check if there are missing or unknown dependencies (warrant warning)."""
        return len(self.missing) > 0 or len(self.unknown) > 0

    def summary(self) -> str:
        """Human-readable summary of dependency status."""
        if self.runnable:
            if not self.all_dependencies:
                return "No dependencies"
            return f"All {len(self.satisfied)} dependencies satisfied"

        parts = []
        if self.unsatisfied:
            nums = ", ".join(f"#{d.issue_number}" for d in self.unsatisfied)
            parts.append(f"waiting on: {nums}")
        if self.missing:
            nums = ", ".join(f"#{d.issue_number}" for d in self.missing)
            parts.append(f"missing: {nums}")
        if self.unknown:
            nums = ", ".join(f"#{d.issue_number}" for d in self.unknown)
            parts.append(f"unknown: {nums}")

        return "Blocked - " + "; ".join(parts)


# Pattern to extract External-ID or issue number from Depends-on lines
# Supports:
#   - Depends-on: #123
#   - Depends-on: M1-010
#   - Depends-on: owner/repo#123
DEPENDS_ON_PATTERN = re.compile(
    r"Depends-on:\s*"
    r"(?:"
    r"(?P<repo>[\w.-]+/[\w.-]+)?#(?P<issue>\d+)"  # owner/repo#123 or #123
    r"|"
    r"(?P<external_id>M\d+-\d+)"  # M1-010 style external ID
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def parse_dependencies(issue_body: str) -> list[tuple[int, str | None]]:
    """Parse dependency references from issue body.

    Returns list of (issue_number, repository) tuples.
    Repository is None for same-repo dependencies.
    """
    dependencies = []

    for match in DEPENDS_ON_PATTERN.finditer(issue_body):
        if match.group("issue"):
            issue_num = int(match.group("issue"))
            repo = match.group("repo")
            dependencies.append((issue_num, repo))
        elif match.group("external_id"):
            # External ID references need to be resolved via lookup
            # For now, we skip these - they require a separate lookup mechanism
            logger.debug(
                "External ID dependency %s found but not yet supported",
                match.group("external_id"),
            )

    return dependencies
