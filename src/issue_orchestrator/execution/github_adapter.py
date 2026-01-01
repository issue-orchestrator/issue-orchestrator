"""GitHub adapter implementing platform port interfaces.

This module provides a GitHubAdapter class that implements the IssueTracker,
LabelSet, and PullRequestTracker protocols using the GitHub HTTP API.

Naming: This is an execution-layer adapter that talks to an external platform.
"""

import logging
import random
import re
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from ..config import Config
from ..ports.issue_tracker import IssueTracker
from ..ports.label_set import LabelSet
from ..ports.pull_request_tracker import PRInfo, PullRequestTracker
from .. import gh_audit
from .github_issue import GitHubIssue
from .github_http import (
    GitHubHttpClient,
    GitHubHttpConfig,
    GitHubHttpError,
    resolve_github_token,
)
from .github_repo import get_repo_from_git, GitRepoError

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey, GitHubIssueKey
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)


class GitHubAdapter:
    """Adapter for GitHub operations via HTTP API.

    This adapter implements the IssueTracker, LabelSet, and PullRequestTracker
    protocols, providing a unified interface for GitHub operations.

    The adapter uses a shared GitHubHttpClient and handles errors gracefully by
    returning None or empty lists on failure.

    Args:
        repo: Repository in owner/repo format (e.g., "owner/repo").
              If None, the current repository is determined from git remote.

    Example:
        >>> adapter = GitHubAdapter("myorg/myrepo")
        >>> issues = adapter.list_issues(labels=["bug"], state="open")
        >>> adapter.add_label(42, "in-progress")
        >>> pr = adapter.create_pr("Fix bug", "This fixes the bug", "feature-branch")
    """

    def __init__(self, repo: str | None = None, config: Config | None = None):
        """Initialize the GitHub adapter.

        Args:
            repo: Repository in owner/repo format. If None, uses current repo.
        """
        if repo:
            self.repo = repo
        else:
            try:
                self.repo = get_repo_from_git()
            except GitRepoError as exc:
                raise GitHubHttpError(f"Failed to resolve repo: {exc}") from exc
        token = resolve_github_token(
            configured_token=getattr(config, "github_token", None) if config else None,
            configured_env=getattr(config, "github_token_env", None) if config else None,
        )
        self._client = GitHubHttpClient(
            GitHubHttpConfig(
                repo=self.repo,
                token=token,
                base_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
                timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
            )
        )
        self._verify_writes = True
        self._verify_timeout_seconds = config.gh_write_verify_timeout_seconds if config else 20
        self._verify_initial_delay_ms = config.gh_write_verify_initial_delay_ms if config else 250
        self._verify_max_delay_ms = config.gh_write_verify_max_delay_ms if config else 2000
        self._verify_backoff = config.gh_write_verify_backoff if config else 1.5
        self._verify_jitter_ms = config.gh_write_verify_jitter_ms if config else 0
        self._label_cache_ttl_seconds = max(0, int(getattr(config, "queue_refresh_seconds", 0))) if config else 0
        self._label_cache: dict[int, tuple[float, list[str]]] = {}
        self._issue_pr_cache: dict[int, PRInfo] = {}
        self._branch_pr_cache: dict[str, PRInfo] = {}
        self._cache_lock = threading.Lock()
        self._cache_wait_count = 0
        self._cache_wait_count_lock = threading.Lock()
        logger.info(f"GitHubAdapter initialized for repo: {self.repo}")

    @contextmanager
    def _cache_guard(self, action: str):
        acquired = self._cache_lock.acquire(blocking=False)
        if not acquired:
            self._increment_cache_waits(action)
            self._cache_lock.acquire()
        try:
            yield
        finally:
            self._cache_lock.release()

    def _increment_cache_waits(self, action: str) -> None:
        with self._cache_wait_count_lock:
            self._cache_wait_count += 1
            wait_count = self._cache_wait_count
        logger.debug("Cache lock contention while %s; wait_count=%d", action, wait_count)

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        if self._label_cache_ttl_seconds <= 0:
            return
        with self._cache_guard("update_label_cache"):
            self._label_cache[issue_number] = (time.monotonic(), list(labels))
        self._update_pr_cache_labels(issue_number, labels)

    def _update_pr_cache_labels(self, issue_number: int, labels: list[str]) -> None:
        with self._cache_guard("update_pr_cache_labels"):
            cached = self._issue_pr_cache.get(issue_number)
            if not cached:
                return
            cached.labels = list(labels)
            if cached.branch:
                self._branch_pr_cache[cached.branch] = cached


    def _cache_pr_info(self, pr_info: PRInfo) -> None:
        issue_number = self._extract_issue_number(pr_info.branch, pr_info.title)
        with self._cache_guard("cache_pr_info"):
            if issue_number is not None:
                self._issue_pr_cache[issue_number] = pr_info
            if pr_info.branch:
                self._branch_pr_cache[pr_info.branch] = pr_info

    def _extract_issue_number(self, branch: str | None, title: str | None) -> int | None:
        if branch:
            match = re.match(r"^(\d+)-", branch)
            if match:
                return int(match.group(1))
        if title:
            match = re.match(r"^#(\d+):", title)
            if match:
                return int(match.group(1))
        return None

    def _verify_write(self, description: str, predicate, detail_fn=None) -> None:
        if not self._verify_writes:
            return
        start = time.monotonic()
        deadline = start + self._verify_timeout_seconds
        delay = self._verify_initial_delay_ms / 1000.0
        max_delay = self._verify_max_delay_ms / 1000.0
        attempt = 0
        last_error: Exception | None = None

        while True:
            attempt += 1
            try:
                if predicate():
                    if attempt > 1:
                        logger.info(
                            "Verified %s after %.2fs (%d attempts)",
                            description,
                            time.monotonic() - start,
                            attempt,
                        )
                    return
            except Exception as e:
                last_error = e

            if time.monotonic() >= deadline:
                break

            sleep_for = min(delay, max_delay)
            if self._verify_jitter_ms > 0:
                sleep_for += random.uniform(0, self._verify_jitter_ms / 1000.0)
            time.sleep(sleep_for)
            delay = min(delay * self._verify_backoff, max_delay)

        detail = ""
        if detail_fn:
            try:
                detail = f" last_state={detail_fn()}"
            except Exception:
                detail = " last_state=<unavailable>"
        if last_error:
            logger.warning(
                "Write verification timed out for %s:%s error=%s",
                description,
                detail,
                last_error,
            )
        else:
            logger.warning("Write verification timed out for %s:%s", description, detail)
        raise GitHubHttpError(f"Timed out verifying write: {description}")

    # IssueRepository implementation

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> "list[Issue]":
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            milestone: Filter by milestone title.
            state: Filter by issue state ("open", "closed", or "all").
            limit: Maximum number of issues to return.

        Returns:
            List of GitHubIssue objects matching the criteria. Returns empty list on error.
        """
        try:
            raw_issues = self._client.list_issues(
                labels=labels,
                state=state,
                milestone=milestone,
                limit=limit,
            )
            return [
                GitHubIssue(
                    number=item["number"],
                    repo=self.repo,
                    title=item.get("title", ""),
                    labels=tuple(label["name"] for label in item.get("labels", [])),
                    state=str(item.get("state", "open")).lower(),
                    body=item.get("body"),
                    milestone=(item.get("milestone") or {}).get("title"),
                    milestone_number=(item.get("milestone") or {}).get("number"),
                    milestone_due_on=(item.get("milestone") or {}).get("due_on"),
                )
                for item in raw_issues
                if isinstance(item, dict)
            ]
        except GitHubHttpError as e:
            logger.error("Failed to list issues: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected error listing issues: %s", e)
            return []

    def get_issue(self, issue_number: int) -> "Issue | None":
        """Get a specific issue by number.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            The GitHubIssue object if found, None otherwise.
        """
        try:
            output = self._client.get_issue(issue_number)
            if isinstance(output, dict):
                milestone_obj = output.get("milestone") or {}
                return GitHubIssue(
                    number=output.get("number", issue_number),
                    repo=self.repo,
                    title=output.get("title", ""),
                    labels=tuple(label["name"] for label in output.get("labels", [])),
                    state=str(output.get("state", "open")).lower(),
                    body=output.get("body"),
                    milestone=milestone_obj.get("title"),
                    milestone_number=milestone_obj.get("number"),
                    milestone_due_on=milestone_obj.get("due_on"),
                )
            return None
        except GitHubHttpError as e:
            logger.error("Failed to get issue %s: %s", issue_number, e)
            return None
        except Exception as e:
            logger.error("Unexpected error getting issue %s: %s", issue_number, e)
            return None

    def get_issue_by_key(self, key: "IssueKey") -> "Issue | None":
        """Get an issue by its IssueKey.

        This is the reverse lookup: IssueKey -> Issue.
        For GitHubIssueKey, extracts the issue number and fetches.

        Args:
            key: The IssueKey to look up.

        Returns:
            The Issue if found, None otherwise.
        """
        from ..domain.issue_key import GitHubIssueKey

        if isinstance(key, GitHubIssueKey):
            # If external_id is numeric, it's the issue number
            if key.external_id.isdigit():
                return self.get_issue(int(key.external_id))
            # Otherwise, we need to search - for now, return None
            # Future: could search by title prefix
            logger.warning(f"Cannot reverse lookup non-numeric external_id: {key}")
            return None
        else:
            logger.warning(f"Cannot lookup non-GitHub IssueKey: {key}")
            return None

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get the labels for a specific issue.

        Args:
            issue_number: The issue number to get labels for.

        Returns:
            List of label names. Returns empty list on error or if issue not found.
        """
        try:
            cached = self._get_cached_labels(issue_number)
            if cached is not None:
                return cached
            return self._get_issue_labels_fresh(issue_number)
        except Exception as e:
            logger.error(f"Failed to get labels for issue {issue_number}: {e}")
            return []

    def _get_cached_labels(self, issue_number: int) -> list[str] | None:
        if self._label_cache_ttl_seconds <= 0:
            return None
        with self._cache_guard("get_cached_labels"):
            entry = self._label_cache.get(issue_number)
            if not entry:
                return None
            fetched_at, labels = entry
            if (time.monotonic() - fetched_at) > self._label_cache_ttl_seconds:
                self._label_cache.pop(issue_number, None)
                return None
            return list(labels)

    def _get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ,
            issue_key=str(issue_number),
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            labels = self._client.get_issue_labels(issue_number)
        self.update_label_cache(issue_number, list(labels))
        return labels

    # LabelManager implementation

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue.

        Args:
            issue_number: The issue number to add the label to.
            label: The label name to add.

        Raises:
            GitHubHttpError: If the operation fails.
        """
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                self._client.add_label(issue_number, label)
            logger.debug(f"Added label '{label}' to issue {issue_number}")
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                last_labels = self._get_issue_labels_fresh(issue_number)
                return label in last_labels

            self._verify_write(
                f"label add #{issue_number}:{label}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
            )
        except GitHubHttpError:
            logger.error(f"Failed to add label '{label}' to issue {issue_number}")
            raise
        finally:
            pass

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue.

        Args:
            issue_number: The issue number to remove the label from.
            label: The label name to remove.

        Raises:
            GitHubHttpError: If the operation fails.
        """
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                self._client.remove_label(issue_number, label)
            logger.debug(f"Removed label '{label}' from issue {issue_number}")
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                last_labels = self._get_issue_labels_fresh(issue_number)
                return label not in last_labels

            self._verify_write(
                f"label remove #{issue_number}:{label}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
            )
        except GitHubHttpError:
            logger.error(f"Failed to remove label '{label}' from issue {issue_number}")
            raise
        finally:
            pass

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if an issue has a specific label.

        Args:
            issue_number: The issue number to check.
            label: The label name to check for.

        Returns:
            True if the issue has the label, False otherwise.
        """
        try:
            labels = self.get_issue_labels(issue_number)
            return label in labels
        except Exception as e:
            logger.error(f"Failed to check label '{label}' on issue {issue_number}: {e}")
            return False

    # PRRepository implementation

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            with self._cache_guard("get_prs_for_branch_cache"):
                cached = self._branch_pr_cache.get(branch)
                if cached and (state == "all" or cached.state.lower() == state.lower()):
                    return [cached]
            output = self._client.get_prs_for_branch(branch, state=state)
            if isinstance(output, list):
                prs = [self._pr_info_from_api(pr) for pr in output if isinstance(pr, dict)]
                for pr_info in prs:
                    self._cache_pr_info(pr_info)
                return prs
            return []
        except GitHubHttpError as e:
            logger.error("Failed to get PRs for branch '%s': %s", branch, e)
            return []
        except Exception as e:
            logger.error("Unexpected error getting PRs for branch '%s': %s", branch, e)
            return []

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Args:
            label: The label name to filter by.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            output = self._client.get_prs_with_label(label, state=state)
            prs: list[PRInfo] = []
            for item in output:
                pr_info = self._fetch_pr_info_from_search(item)
                if pr_info:
                    prs.append(pr_info)
            return prs
        except GitHubHttpError as e:
            logger.error("Failed to get PRs with label '%s': %s", label, e)
            return []
        except Exception as e:
            logger.error("Unexpected error getting PRs with label '%s': %s", label, e)
            return []

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get all pull requests associated with a specific issue.

        Finds PRs where:
        - Branch starts with the issue number followed by a dash (e.g., "328-feature-name")
        - OR title contains "#issue_number" (e.g., "#328: Feature")

        Args:
            issue_number: The issue number to find PRs for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            with self._cache_guard("get_prs_for_issue_cache"):
                cached = self._issue_pr_cache.get(issue_number)
                if cached and (state == "all" or cached.state.lower() == state.lower()):
                    return [cached]
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                prs = self._client.get_prs_for_issue(issue_number)
            pr_infos: list[PRInfo] = []
            for pr in prs:
                pr_info = self._fetch_pr_info_from_search(pr)
                if pr_info:
                    pr_infos.append(pr_info)
            for pr_info in pr_infos:
                self._cache_pr_info(pr_info)
            return pr_infos
        except GitHubHttpError as e:
            logger.error("Failed to get PRs for issue %s: %s", issue_number, e)
            return []
        except Exception as e:
            logger.error("Unexpected error getting PRs for issue %s: %s", issue_number, e)
            return []

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        Args:
            pr_number: The PR number to retrieve.

        Returns:
            The PRInfo object if found, None otherwise.
        """
        try:
            output = self._client.get_pr(pr_number)
            if isinstance(output, dict):
                return self._pr_info_from_api(output)
            return None
        except GitHubHttpError as e:
            logger.error("Failed to get PR %s: %s", pr_number, e)
            return None
        except Exception as e:
            logger.error("Unexpected error getting PR %s: %s", pr_number, e)
            return None

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List pull requests.

        Args:
            state: Filter by PR state ("open", "closed", "merged", or "all").
            limit: Maximum number of PRs to return.

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            output = self._client.list_prs(state=state, limit=limit)
            if isinstance(output, list):
                return [self._pr_info_from_api(pr) for pr in output if isinstance(pr, dict)]
            return []
        except GitHubHttpError as e:
            logger.error("Failed to list PRs: %s", e)
            return []
        except Exception as e:
            logger.error("Unexpected error listing PRs: %s", e)
            return []

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PRInfo:
        """Create a new pull request, or return existing PR if one exists for the branch.

        This operation is idempotent - if a PR already exists for the given branch,
        it returns that PR instead of failing.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch).

        Returns:
            A PRInfo object representing the created or existing PR.

        Raises:
            GitHubHttpError: If there's an error creating the PR and no existing PR found.
        """
        # First, check if a PR already exists for this branch
        existing_prs = self.get_prs_for_branch(head)
        if existing_prs:
            existing_pr = existing_prs[0]
            logger.info(f"PR already exists for branch {head}: #{existing_pr.number}")
            self._cache_pr_info(existing_pr)
            return existing_pr

        try:
            logger.info("Creating PR via GitHub API: repo=%s head=%s base=%s", self.repo, head, base)
            output = self._client.create_pr(title=title, body=body, head=head, base=base)
            if isinstance(output, dict):
                pr_info = self._pr_info_from_api(output)
                logger.info("Created PR #%s: %s", pr_info.number, pr_info.title)
                self._cache_pr_info(pr_info)
                last_pr: PRInfo | None = pr_info

                def _check() -> bool:
                    nonlocal last_pr
                    last_pr = self.get_pr(pr_info.number)
                    return last_pr is not None

                self._verify_write(
                    f"pr create #{pr_info.number}:{head}",
                    _check,
                    detail_fn=lambda: {
                        "number": last_pr.number if last_pr else None,
                        "branch": last_pr.branch if last_pr else None,
                    },
                )
                return pr_info
            raise GitHubHttpError("Failed to parse PR create response")
        except GitHubHttpError as e:
            logger.error("Failed to create PR: title=%s head=%s base=%s error=%s", title, head, base, e)
            raise

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment to an issue or pull request.

        Args:
            issue_or_pr_number: The issue or PR number to comment on.
            body: The comment text to add.

        Returns:
            The URL of the created comment.

        Raises:
            GitHubHttpError: If there's an error adding the comment.
        """
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_WRITE,
                issue_key=str(issue_or_pr_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                comment_url = self._client.add_comment(issue_or_pr_number, body)
            logger.debug(f"Added comment to issue/PR {issue_or_pr_number}")

            last_comments: list[dict] = []

            def _check() -> bool:
                nonlocal last_comments
                last_comments = self._client.get_issue_comments(issue_or_pr_number)
                return any(c.get("body") == body for c in last_comments)

            self._verify_write(
                f"comment add #{issue_or_pr_number}",
                _check,
                detail_fn=lambda: {"comment_count": len(last_comments)},
            )

            return comment_url
        except GitHubHttpError:
            logger.error(f"Failed to add comment to issue/PR {issue_or_pr_number}")
            raise

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found).

        This method implements the IssueStateChecker protocol for dependency evaluation.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo dependencies.
                  If None, uses this adapter's configured repo.

        Returns:
            The issue state ('open' or 'closed'), or None if the issue cannot be found.
        """
        target_repo = repo or self.repo
        try:
            if target_repo != self.repo:
                temp_client = GitHubHttpClient(
                    GitHubHttpConfig(
                        repo=target_repo,
                        token=self._client._config.token,
                        base_url=self._client._config.base_url,
                        timeout_seconds=self._client._config.timeout_seconds,
                    )
                )
                output = temp_client.get_issue(issue_number)
                temp_client.close()
            else:
                output = self._client.get_issue(issue_number)

            if isinstance(output, dict):
                return output.get("state")
            return None
        except GitHubHttpError as e:
            logger.debug("Issue %s in %s not found: %s", issue_number, target_repo, e)
            return None
        except Exception as e:
            logger.debug("Error checking issue %s in %s: %s", issue_number, target_repo, e)
            raise

    def create_issue_key(self, issue_number: int) -> "GitHubIssueKey":
        """Create a GitHubIssueKey for the given issue number.

        This allows the orchestrator to get IssueKeys without knowing about
        GitHub-specific implementations.

        The method fetches the issue to extract the stable external_id from the
        title (e.g., "[M1-011] Fix login bug" -> external_id="M1-011").
        Falls back to using the issue number as external_id if the issue can't
        be fetched or has no external_id prefix in its title.

        Args:
            issue_number: The issue number to create a key for.

        Returns:
            A GitHubIssueKey with this adapter's repo and the parsed external_id.
        """
        from ..domain.issue_key import GitHubIssueKey, parse_external_id

        # Try to fetch the issue to get the stable external_id from title
        issue = self.get_issue(issue_number)
        if issue:
            parsed = parse_external_id(issue.title)
            if parsed.external_id:
                return GitHubIssueKey(repo=self.repo, external_id=parsed.external_id)

        # Fall back to issue number if no external_id found
        return GitHubIssueKey(repo=self.repo, external_id=str(issue_number))

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create a new issue.

        Args:
            title: Issue title
            body: Issue body
            labels: Labels to add

        Returns:
            Issue number if created, None on failure
        """
        issue_number = self._client.create_issue(title=title, body=body, labels=labels)
        if issue_number is None:
            return None
        if labels:
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                last_labels = self._get_issue_labels_fresh(issue_number)
                return all(label in last_labels for label in labels)

            self._verify_write(
                f"issue create #{issue_number}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
            )
        return issue_number

    def get_rate_limit_snapshot(self) -> dict[str, Any] | None:
        snapshot = self._client.get_rate_limit_snapshot()
        return snapshot.to_payload() if snapshot else None

    def get_token_scopes(self) -> list[str]:
        return self._client.get_token_scopes()

    def _pr_info_from_api(self, pr: dict[str, Any]) -> PRInfo:
        raw_number = pr.get("number")
        if raw_number is None:
            number = 0
        else:
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                number = 0
        labels: list[str] = []
        for label in pr.get("labels", []):
            if not isinstance(label, dict):
                continue
            name = label.get("name")
            if isinstance(name, str):
                labels.append(name)
        return PRInfo(
            number=number,
            title=pr.get("title", ""),
            url=pr.get("html_url") or pr.get("url", ""),
            branch=(pr.get("head") or {}).get("ref", pr.get("headRefName", "")),
            body=pr.get("body", "") or "",
            state=str(pr.get("state", "open")).lower(),
            labels=labels,
        )

    def _fetch_pr_info_from_search(self, pr: dict[str, Any]) -> PRInfo | None:
        if not isinstance(pr, dict):
            return None
        pr_number = pr.get("number")
        if not pr_number:
            return None
        full = self._client.get_pr(pr_number)
        if not isinstance(full, dict):
            return None
        return self._pr_info_from_api(full)
