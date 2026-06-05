"""Adapter-level GitHub cache coordination helpers."""

from __future__ import annotations

import logging
import re
from typing import Any

from ...ports.pull_request_tracker import PRInfo
from .cache import GitHubCache

logger = logging.getLogger(__name__)


class GitHubAdapterCacheSupport:
    """Own adapter-specific cache rules around the low-level GitHubCache."""

    def __init__(self, cache: GitHubCache, *, label_cache_enabled: bool) -> None:
        self._cache = cache
        self._label_cache_enabled = label_cache_enabled

    def get_cached_labels(self, issue_number: int) -> list[str] | None:
        """Get cached labels for an issue, or None if not cached/stale."""
        if not self._label_cache_enabled:
            return None
        return self._cache.get_issue_labels(issue_number)

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        """Update cached labels for an issue and matching cached PR."""
        if not self._label_cache_enabled:
            return
        self._cache.set_issue_labels(issue_number, list(labels))
        self._update_pr_cache_labels(issue_number, labels)

    def invalidate_label_cache(self, issue_number: int) -> None:
        """Invalidate cached labels for an issue."""
        self._cache.invalidate_issue_labels(issue_number)
        logger.debug("Invalidated label cache for issue %d", issue_number)

    def invalidate_pr_cache(
        self,
        *,
        pr_number: int | None = None,
        issue_number: int | None = None,
        branch: str | None = None,
    ) -> None:
        """Invalidate cached PR info by issue number and/or branch."""
        if pr_number is not None:
            self._cache.invalidate_pr(pr_number)
            logger.debug("Invalidated PR cache for PR %d", pr_number)
        if issue_number is not None:
            self._cache.invalidate_pr_by_issue(issue_number)
            logger.debug("Invalidated PR cache for issue %d", issue_number)
        if branch is not None:
            self._cache.invalidate_pr_by_branch(branch)
            logger.debug("Invalidated PR cache for branch %s", branch)

    def get_cached_pr_for_branch(self, branch: str, state: str) -> PRInfo | None:
        """Return cached PR info for a branch when it satisfies the requested state."""
        cached = self._cache.get_pr_by_branch(branch)
        if not cached:
            return None
        pr_info = self._pr_info_from_cache(cached)
        if pr_info and self._state_matches(pr_info, state):
            return pr_info
        return None

    def get_cached_pr_for_issue(self, issue_number: int, state: str) -> PRInfo | None:
        """Return cached PR info for an issue when it satisfies the requested state."""
        cached = self._cache.get_pr_by_issue(issue_number)
        if not cached:
            return None
        pr_info = self._pr_info_from_cache(cached)
        if pr_info and self._state_matches(pr_info, state):
            return pr_info
        return None

    def cache_pr_info(self, pr_info: PRInfo) -> None:
        """Cache PR info by issue number when possible, otherwise by branch."""
        issue_number = self._extract_issue_number(pr_info.branch, pr_info.title)
        pr_data = {
            "number": pr_info.number,
            "branch": pr_info.branch,
            "title": pr_info.title,
            "labels": list(pr_info.labels) if pr_info.labels else [],
            "url": pr_info.url,
            "body": pr_info.body,
            "state": pr_info.state,
            "issue_number": issue_number,
        }
        if issue_number is not None:
            self._cache.set_pr_by_issue(issue_number, pr_data, branch=pr_info.branch)
        elif pr_info.branch:
            self._cache.set_pr_by_branch(pr_info.branch, pr_data)

    def _update_pr_cache_labels(self, issue_number: int, labels: list[str]) -> None:
        """Update labels on a cached PR."""
        cached = self._cache.get_pr_by_issue(issue_number)
        if not cached:
            return
        cached["labels"] = list(labels)
        branch = cached.get("branch")
        self._cache.set_pr_by_issue(issue_number, cached, branch=branch)

    @staticmethod
    def _state_matches(pr_info: PRInfo, state: str) -> bool:
        return state == "all" or pr_info.state.lower() == state.lower()

    @staticmethod
    def _pr_info_from_cache(cached: dict[str, Any]) -> PRInfo | None:
        """Convert cached PR data back to PRInfo."""
        if not cached:
            return None
        return PRInfo(
            number=cached.get("number", 0),
            title=cached.get("title", ""),
            url=cached.get("url", ""),
            branch=cached.get("branch", ""),
            body=cached.get("body", ""),
            state=cached.get("state", "open"),
            labels=cached.get("labels", []),
        )

    @staticmethod
    def _extract_issue_number(branch: str | None, title: str | None) -> int | None:
        if branch:
            match = re.match(r"^(\d+)-", branch)
            if match:
                return int(match.group(1))
        if title:
            match = re.match(r"^#(\d+):", title)
            if match:
                return int(match.group(1))
        return None
