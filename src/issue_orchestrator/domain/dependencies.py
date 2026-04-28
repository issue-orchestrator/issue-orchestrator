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

from .issue_key import ISSUE_LABEL_SEPARATOR

logger = logging.getLogger(__name__)


class DependencyState(Enum):
    """State of a single dependency."""

    SATISFIED = "satisfied"  # Dependency issue is CLOSED
    UNSATISFIED = "unsatisfied"  # Dependency issue is OPEN
    MISSING = "missing"  # Cannot find dependency (404, permissions, deleted)
    UNKNOWN = "unknown"  # Transient error (network, rate limit)
    CROSS_MILESTONE = "cross_milestone"  # Dependency violates milestone scope


@dataclass(frozen=True)
class Dependency:
    """A single dependency reference."""

    # The issue number being depended on (after resolution)
    issue_number: int | None

    # Optional: repository in owner/repo format (for cross-repo deps)
    repository: str | None = None

    # Original external_id if referenced via M1-010 style
    external_id: str | None = None

    # Resolved state
    state: DependencyState = DependencyState.UNKNOWN

    # Error message if missing/unknown/cross_milestone
    error: str | None = None

    # Milestone of the dependency issue (for cross-milestone validation)
    milestone: str | None = None

    @property
    def is_satisfied(self) -> bool:
        return self.state == DependencyState.SATISFIED

    @property
    def blocks_running(self) -> bool:
        """Check if this dependency blocks the issue from running."""
        return self.state != DependencyState.SATISFIED

    @property
    def display_ref(self) -> str:
        """Human-readable reference for logging/display.

        When both a logical key (external_id) and a backing-store number are
        known, show both ("M9-009 · #274") so the same dependency reads the
        same here as in dashboard cards. Cross-repo deps keep the owner/repo
        prefix on the numeric part.
        """
        if self.issue_number:
            number_part = (
                f"{self.repository}#{self.issue_number}"
                if self.repository
                else f"#{self.issue_number}"
            )
            if self.external_id:
                return f"{self.external_id}{ISSUE_LABEL_SEPARATOR}{number_part}"
            return number_part
        if self.external_id:
            return self.external_id
        return "(unknown)"


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
    cross_milestone: tuple[Dependency, ...] = field(default_factory=tuple)

    @property
    def runnable(self) -> bool:
        """Issue is runnable only if ALL dependencies are satisfied."""
        return (
            len(self.unsatisfied) == 0
            and len(self.missing) == 0
            and len(self.unknown) == 0
            and len(self.cross_milestone) == 0
        )

    @property
    def all_dependencies(self) -> tuple[Dependency, ...]:
        """All dependencies regardless of state."""
        return self.satisfied + self.unsatisfied + self.missing + self.unknown + self.cross_milestone

    @property
    def blocking_dependencies(self) -> tuple[Dependency, ...]:
        """Dependencies that block running."""
        return self.unsatisfied + self.missing + self.unknown + self.cross_milestone

    @property
    def has_cross_milestone(self) -> bool:
        """Check if there are cross-milestone dependency violations."""
        return len(self.cross_milestone) > 0

    @property
    def has_warnings(self) -> bool:
        """Check if there are missing, unknown, or cross-milestone dependencies."""
        return len(self.missing) > 0 or len(self.unknown) > 0 or len(self.cross_milestone) > 0

    def summary(self) -> str:
        """Human-readable summary of dependency status."""
        if self.runnable:
            if not self.all_dependencies:
                return "No dependencies"
            return f"All {len(self.satisfied)} dependencies satisfied"

        parts = []
        if self.unsatisfied:
            refs = ", ".join(d.display_ref for d in self.unsatisfied)
            parts.append(f"waiting on: {refs}")
        if self.missing:
            refs = ", ".join(d.display_ref for d in self.missing)
            parts.append(f"missing: {refs}")
        if self.unknown:
            refs = ", ".join(d.display_ref for d in self.unknown)
            parts.append(f"unknown: {refs}")
        if self.cross_milestone:
            refs = ", ".join(d.display_ref for d in self.cross_milestone)
            parts.append(f"cross-milestone: {refs}")

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
    r"(?P<external_id>M\d+-\d{3})"  # M1-010 style external ID (3 digits)
    r")",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class ParsedDependencyRef:
    """A parsed dependency reference before resolution.

    Either issue_number or external_id will be set, not both.
    """

    # Issue number if referenced via #123 or owner/repo#123
    issue_number: int | None = None

    # External ID if referenced via M1-010 style
    external_id: str | None = None

    # Repository for cross-repo dependencies (owner/repo format)
    repository: str | None = None


def parse_dependencies(issue_body: str) -> list[tuple[int, str | None]]:
    """Parse dependency references from issue body.

    Returns list of (issue_number, repository) tuples.
    Repository is None for same-repo dependencies.

    Note: This is the legacy interface that only returns issue number refs.
    Use parse_dependency_refs() for the full interface including external IDs.
    """
    dependencies = []

    for match in DEPENDS_ON_PATTERN.finditer(issue_body):
        if match.group("issue"):
            issue_num = int(match.group("issue"))
            repo = match.group("repo")
            dependencies.append((issue_num, repo))
        elif match.group("external_id"):
            # External ID references need resolution - use parse_dependency_refs()
            logger.debug(
                "External ID dependency %s found - use parse_dependency_refs() for full support",
                match.group("external_id"),
            )

    return dependencies


def parse_dependency_refs(issue_body: str) -> list[ParsedDependencyRef]:
    """Parse all dependency references from issue body.

    Returns ParsedDependencyRef objects that can reference issues by:
    - Issue number (#123, owner/repo#123)
    - External ID (M1-010)

    External IDs require resolution via IssueResolver before state checking.
    """
    refs: list[ParsedDependencyRef] = []

    for match in DEPENDS_ON_PATTERN.finditer(issue_body):
        if match.group("issue"):
            refs.append(
                ParsedDependencyRef(
                    issue_number=int(match.group("issue")),
                    repository=match.group("repo"),
                )
            )
        elif match.group("external_id"):
            refs.append(
                ParsedDependencyRef(
                    external_id=match.group("external_id").upper(),
                )
            )

    return refs
