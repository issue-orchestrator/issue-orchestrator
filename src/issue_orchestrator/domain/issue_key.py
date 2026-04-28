"""IssueKey - stable identity for work items.

An IssueKey answers exactly one question:
"How do I refer to a unit of work in a way that is stable across backing stores?"

This is NOT about:
- How it's stored
- How it's fetched
- How it's mutated

IssueKey is an interface, not a data structure. This enables:
- GitHub Issues
- Database-backed issues
- Imported backlogs
- Archived snapshots

Identity equality is structural, not referential:
Two keys with same scope + stable_id are the same work item,
even if they resolve to different backing-store locators over time.

Usage:
    # Domain logic speaks only in IssueKeys
    def is_blocked(key: IssueKey, resolver: IssueResolver) -> bool:
        handle = resolver.resolve(key)
        return handle.state == "open"
"""

import re
from dataclasses import dataclass
from typing import NewType, Protocol, runtime_checkable

StableIssueId = NewType("StableIssueId", str)


@runtime_checkable
class IssueKey(Protocol):
    """Protocol for stable work item identity.

    This is a thin identity protocol - no fetching, resolving, or persistence.
    Resolution belongs to IssueResolver, not here.
    """

    def stable_id(self) -> StableIssueId:
        """Stable, human-meaningful identifier (e.g. 'M1-011')."""
        ...

    def scope(self) -> str:
        """Namespace / repository / project boundary."""
        ...

    def __str__(self) -> str:
        """Human-readable representation."""
        ...

    def __hash__(self) -> int:
        """Must be hashable for use as dict key."""
        ...

    def __eq__(self, other: object) -> bool:
        """Structural equality based on scope + stable_id."""
        ...


@dataclass(frozen=True)
class GitHubIssueKey:
    """IssueKey implementation for GitHub-backed issues.

    Attributes:
        repo: Repository in owner/repo format
        external_id: The stable ID from title prefix (e.g. "M1-011")
    """

    repo: str
    external_id: str  # M1-011

    def stable_id(self) -> StableIssueId:
        return StableIssueId(self.external_id)

    def scope(self) -> str:
        return self.repo

    def __str__(self) -> str:
        return f"{self.repo}:{self.external_id}"


@dataclass(frozen=True)
class FakeIssueKey:
    """IssueKey implementation for testing.

    Makes planner and dependency tests trivial - no GitHub needed.
    """

    name: str
    test_scope: str = "test"

    def stable_id(self) -> StableIssueId:
        return StableIssueId(self.name)

    def scope(self) -> str:
        return self.test_scope

    def __str__(self) -> str:
        return f"test:{self.name}"


# Type alias for issue handle (backing-store locator)
# GitHub -> issue number, DB -> row ID, etc.
IssueHandle = int | str | None


# =============================================================================
# Title Parsing - Extract external_id from issue titles
# =============================================================================

# Pattern to match [M1-011] style prefixes in issue titles
EXTERNAL_ID_PATTERN = re.compile(r"^\[(M\d+-\d{3})\]")


@dataclass(frozen=True)
class ParsedTitle:
    """Result of parsing an issue title.

    Attributes:
        external_id: The extracted external ID (e.g. "M1-011"), or None
        raw_title: The remaining title after the prefix
    """

    external_id: str | None
    raw_title: str


# =============================================================================
# Display Labels - Combine logical key + backing-store handle for UI
# =============================================================================

# Separator between the logical key and the GitHub number in display labels
# (e.g., "M9-009 · #274"). Centralized so the look can be tweaked in one place.
ISSUE_LABEL_SEPARATOR = " · "


def issue_label_parts(issue_key: str | None, issue_number: int | None) -> tuple[str, ...]:
    """Order parts of the display label.

    Swap the order of the appended parts here to flip key-first vs number-first
    everywhere the label is rendered.
    """
    parts: list[str] = []
    if issue_key:
        parts.append(issue_key)
    if issue_number is not None:
        parts.append(f"#{issue_number}")
    return tuple(parts)


def format_issue_label(issue_number: int | None, issue_key: str | None) -> str:
    """Render an issue display label combining logical key and GitHub number.

    Examples:
        format_issue_label(274, "M9-009") -> "M9-009 · #274"
        format_issue_label(274, None)     -> "#274"
        format_issue_label(None, "M9-009") -> "M9-009"
        format_issue_label(None, None)    -> ""
    """
    return ISSUE_LABEL_SEPARATOR.join(issue_label_parts(issue_key, issue_number))


def parse_external_id(title: str) -> ParsedTitle:
    """Parse external ID from an issue title.

    Extracts [M1-011] style prefixes from issue titles.

    Args:
        title: The full issue title

    Returns:
        ParsedTitle with external_id (if found) and remaining title

    Examples:
        >>> parse_external_id("[M1-011] Fix login bug")
        ParsedTitle(external_id='M1-011', raw_title='Fix login bug')

        >>> parse_external_id("Regular issue without prefix")
        ParsedTitle(external_id=None, raw_title='Regular issue without prefix')
    """
    title = title.strip()
    match = EXTERNAL_ID_PATTERN.match(title)

    if not match:
        return ParsedTitle(external_id=None, raw_title=title)

    external_id = match.group(1)
    raw_title = title[match.end():].strip()
    return ParsedTitle(external_id=external_id, raw_title=raw_title)
