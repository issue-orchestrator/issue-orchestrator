"""GitHub issue resolver - translates IssueKeys to GitHub issue numbers.

This is the GitHub-specific implementation of IssueResolver.
It maintains an in-memory cache of external_id -> issue_number
mappings, built by scanning issues from the IssueTracker.

For other backing stores:
- DBIssueResolver would resolve to row IDs
- FileIssueResolver would resolve to file paths

Architecture:
- Parsing is domain (parse_external_id)
- Resolution is control+ports (this module)
- GitHub access is only through adapters (IssueTracker)
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.issue_key import IssueKey, IssueHandle, GitHubIssueKey, parse_external_id
from ..events import EventName
from ..ports import IssueTracker, EventSink, TraceEvent
from ..infra import gh_audit

if TYPE_CHECKING:
    from ..models import Issue

logger = logging.getLogger(__name__)


@dataclass
class GitHubIssueResolver:
    """Resolves IssueKeys to GitHub issue numbers via cached lookup.

    This is the GitHub-specific IssueResolver implementation.
    The IssueHandle returned is always int (GitHub issue number).

    The cache is built by scanning issues and extracting external_ids
    from their titles. This is done once at startup and can be rebuilt
    on demand.

    Attributes:
        repo: The repository in owner/repo format
        issue_tracker: IssueTracker port for fetching issues
        events: EventSink for trace events
    """

    repo: str
    issue_tracker: IssueTracker
    events: EventSink

    # Cache: external_id -> issue_number
    _cache: dict[str, int] = field(default_factory=dict, init=False)

    # Track duplicates for warnings
    _duplicates: dict[str, list[int]] = field(default_factory=dict, init=False)

    def resolve(self, key: IssueKey) -> IssueHandle:
        """Resolve an IssueKey to its GitHub issue number.

        Uses the cache first, rebuilds if not found.

        Args:
            key: The IssueKey to resolve

        Returns:
            The GitHub issue number (int), or None if not found
        """
        external_id = key.stable_id()

        # Fast path: check cache
        if external_id in self._cache:
            return self._cache[external_id]

        # Slow path: rebuild index and try again
        logger.debug("Cache miss for %s, rebuilding index", external_id)
        self.build_index()

        return self._cache.get(external_id)

    def build_index(self) -> None:
        """Rebuild the resolution cache by scanning issues.

        Fetches issues from the tracker, parses titles to extract
        external_ids, and builds the mapping.
        """
        logger.info("Building issue resolution index for %s", self.repo)

        # Fetch all relevant issues (open + recently closed)
        with gh_audit.context(
            reason=gh_audit.AuditReason.EXTERNAL_ID_RESOLVE,
            scope=gh_audit.AuditScope.ON_DEMAND,
        ):
            issues = self.issue_tracker.list_issues(state="all", limit=500)

        new_cache: dict[str, int] = {}
        new_duplicates: dict[str, list[int]] = {}

        for issue in issues:
            parsed = parse_external_id(issue.title)
            if parsed.external_id:
                ext_id = parsed.external_id

                if ext_id in new_cache:
                    # Duplicate found - track for warning
                    if ext_id not in new_duplicates:
                        new_duplicates[ext_id] = [new_cache[ext_id]]
                    new_duplicates[ext_id].append(issue.number)
                else:
                    new_cache[ext_id] = issue.number

        self._cache = new_cache
        self._duplicates = new_duplicates

        # Warn about duplicates
        for ext_id, numbers in self._duplicates.items():
            logger.warning(
                "Duplicate external_id %s found in issues: %s",
                ext_id,
                numbers,
            )
            self.events.publish(
                TraceEvent(
                    EventName.RESOLVER_DUPLICATE_EXTERNAL_ID,
                    {
                        "external_id": ext_id,
                        "issue_numbers": numbers,
                    },
                )
            )

        logger.info(
            "Index built: %d issues indexed, %d duplicates",
            len(new_cache),
            len(new_duplicates),
        )

    def invalidate(self, key: IssueKey) -> None:
        """Invalidate a cached resolution.

        Args:
            key: The IssueKey to invalidate
        """
        external_id = key.stable_id()
        self._cache.pop(external_id, None)
        logger.debug("Invalidated cache for %s", external_id)

    def get_key_for_issue(self, issue: "Issue") -> GitHubIssueKey | None:
        """Create an IssueKey from an Issue if it has an external_id.

        Args:
            issue: The Issue to extract a key from

        Returns:
            GitHubIssueKey if the issue has an external_id, None otherwise
        """
        parsed = parse_external_id(issue.title)
        if parsed.external_id:
            return GitHubIssueKey(repo=self.repo, external_id=parsed.external_id)
        return None

    def get_all_keys(self) -> list[GitHubIssueKey]:
        """Get all known IssueKeys from the cache.

        Returns:
            List of GitHubIssueKey instances
        """
        return [
            GitHubIssueKey(repo=self.repo, external_id=ext_id)
            for ext_id in self._cache.keys()
        ]
