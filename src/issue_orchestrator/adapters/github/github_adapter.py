"""GitHub adapter implementing platform port interfaces.

This module provides a GitHubAdapter class that implements the IssueTracker,
LabelSet, and PullRequestTracker protocols using the GitHub HTTP API.

Naming: This is an execution-layer adapter that talks to an external platform.
"""

import logging
import os
import time
from typing import TYPE_CHECKING, Any

from ...infra.config import Config
from ...ports.pull_request_tracker import PRInfo, PRRef, StatusCheckRollupState
from ...infra import gh_audit
from .github_issue import GitHubIssue
from .errors import GitHubHttpError, GitHubTransportError
from .http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
)
from .tokens import resolve_github_token
from .repo import get_repo_from_git, GitRepoError
from .cache import GitHubCache
from .adapter_cache import GitHubAdapterCacheSupport
from ...ports.verification import VerificationService

if TYPE_CHECKING:
    from ...domain.issue_key import IssueKey, GitHubIssueKey
    from ...ports.issue import Issue

logger = logging.getLogger(__name__)


_VALID_ROLLUP_STATES: frozenset[str] = frozenset(
    {"SUCCESS", "FAILURE", "PENDING", "EXPECTED", "ERROR"}
)


def _is_not_found_error(exc: GitHubHttpError) -> bool:
    return exc.status_code == 404


def _coerce_rollup_state(raw: object) -> StatusCheckRollupState | None:
    if not isinstance(raw, str):
        return None
    upper = raw.upper()
    if upper in _VALID_ROLLUP_STATES:
        return upper  # type: ignore[return-value]
    logger.warning("Unknown statusCheckRollup state from GitHub: %r", raw)
    return None


class GitHubAdapter:
    """Adapter for GitHub operations via HTTP API.

    This adapter implements the IssueTracker, LabelSet, and PullRequestTracker
    protocols, providing a unified interface for GitHub operations.

    The adapter uses a shared GitHubHttpClient. Expected absence is represented
    as None or an empty list; GitHub access failures propagate so callers can
    distinguish an empty result from an upstream failure.

    Args:
        repo: Repository in owner/repo format (e.g., "owner/repo").
              If None, the current repository is determined from git remote.

    Example:
        >>> adapter = GitHubAdapter("myorg/myrepo")
        >>> issues = adapter.list_issues(labels=["bug"], state="open")
        >>> adapter.add_label(42, "in-progress")
        >>> pr = adapter.create_pr("Fix bug", "This fixes the bug", "feature-branch")
    """

    def __init__(
        self,
        repo: str | None = None,
        config: Config | None = None,
        cache: GitHubCache | None = None,
        verification_service: VerificationService | None = None,
        http_client: "GitHubHttpClient | None" = None,
        verify_writes: bool = True,
    ):
        """Initialize the GitHub adapter.

        Args:
            repo: Repository in owner/repo format. If None, uses current repo.
            config: Configuration object.
            cache: GitHubCache instance for caching API responses. If None, one is created.
            verification_service: VerificationService for write-verify patterns. If None, one is created.
                                  The service should be injected to preserve circuit breaker state.
            http_client: GitHubHttpClient instance. If None, one is created.
                         Inject for testing to avoid real API calls.
            verify_writes: Whether to verify writes. Defaults to True.
        """
        if repo:
            self.repo = repo
        else:
            try:
                self.repo = get_repo_from_git()
            except GitRepoError as exc:
                raise GitHubHttpError(f"Failed to resolve repo: {exc}") from exc
        auth_kwargs = config.github_auth_kwargs() if config else {}
        token = resolve_github_token(
            **auth_kwargs,
            api_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
        )
        if http_client is not None:
            self._client = http_client
        else:
            self._client = GitHubHttpClient(
                GitHubHttpConfig(
                    repo=self.repo,
                    token=token,
                    base_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
                    timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
                )
            )
        self._verify_writes = verify_writes
        self._verify_timeout_seconds = config.gh_write_verify_timeout_seconds if config else 20
        self._verify_initial_delay_ms = config.gh_write_verify_initial_delay_ms if config else 250
        self._verify_max_delay_ms = config.gh_write_verify_max_delay_ms if config else 2000
        self._verify_backoff = config.gh_write_verify_backoff if config else 1.5
        self._verify_jitter_ms = config.gh_write_verify_jitter_ms if config else 0

        # Use injected cache or create one with config-based TTL
        cache_ttl = float(max(0, int(getattr(config, "github_cache_ttl_seconds", 0)))) if config else 0.0
        self._cache = cache if cache is not None else GitHubCache(default_ttl=cache_ttl)
        self._adapter_cache = GitHubAdapterCacheSupport(self._cache, label_cache_enabled=cache_ttl > 0)

        # Use injected verification service or create one with default budget
        # IMPORTANT: Inject the service to preserve circuit breaker state across calls
        if verification_service is not None:
            self._verification_service = verification_service
        else:
            from ...execution.verification_service import DefaultVerificationService, VerificationBudget
            default_budget = VerificationBudget(
                timeout_seconds=self._verify_timeout_seconds,
                max_attempts=20,
                initial_delay_ms=self._verify_initial_delay_ms,
                max_delay_ms=self._verify_max_delay_ms,
                backoff_factor=self._verify_backoff,
                jitter_ms=self._verify_jitter_ms,
            )
            self._verification_service = DefaultVerificationService(default_budget=default_budget)

        logger.info(f"GitHubAdapter initialized for repo: {self.repo}")

    @property
    def http_client(self) -> "GitHubHttpClient":
        """Expose the HTTP client for use by other adapters (e.g., ClaimAdapter)."""
        return self._client

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        """Update cached labels for an issue."""
        self._adapter_cache.update_label_cache(issue_number, labels)

    def invalidate_label_cache(self, issue_number: int) -> None:
        """Invalidate cached labels for an issue.

        Call this after any write operation that modifies labels.
        Per architecture: explicit invalidation rules for cache.
        """
        self._adapter_cache.invalidate_label_cache(issue_number)
        self._adapter_cache.invalidate_pr_cache(
            pr_number=issue_number,
            issue_number=issue_number,
        )
        self._client.invalidate_pr_etag(issue_number)

    def invalidate_pr_cache(self, issue_number: int | None = None, branch: str | None = None) -> None:
        """Invalidate cached PR info.

        Args:
            issue_number: Invalidate PR cache for this issue.
            branch: Invalidate PR cache for this branch.
        """
        self._adapter_cache.invalidate_pr_cache(issue_number=issue_number, branch=branch)

    def _verify_write(self, description: str, predicate, detail_fn=None, issue_number: int | None = None) -> None:
        """Verify a write operation completed successfully.

        Uses the injected VerificationService which maintains circuit breaker
        state across calls. The service is initialized in __init__.

        Failure classification:
        - SYSTEMIC: Timeout or API error -> orchestrator should pause, probe, resume
        - ISSUE_LOCAL: Predicate false after retries -> apply needs-reconcile label

        Args:
            description: Human-readable description of the write operation.
            predicate: Callable that returns True if the write is verified.
            detail_fn: Optional callable that returns the last observed state.
            issue_number: Optional issue number for issue-local failure classification.
        """
        if not self._verify_writes:
            return

        from ...execution.verification_service import VerificationBudget
        from ...ports.verification import VerificationResult

        # Create per-call budget (configuration, not state)
        budget = VerificationBudget(
            timeout_seconds=self._verify_timeout_seconds,
            max_attempts=20,  # Allow more attempts within time budget
            initial_delay_ms=self._verify_initial_delay_ms,
            max_delay_ms=self._verify_max_delay_ms,
            backoff_factor=self._verify_backoff,
            jitter_ms=self._verify_jitter_ms,
        )

        api_error_occurred = False

        def check() -> tuple[bool, str | None]:
            """Check predicate and return (success, observed_state)."""
            nonlocal api_error_occurred
            try:
                success = predicate()
                observed = None
                if detail_fn:
                    try:
                        observed = str(detail_fn())
                    except Exception:
                        observed = "<unavailable>"
                return (success, observed)
            except GitHubHttpError:
                # API error during verification -> systemic failure
                api_error_occurred = True
                raise
            except Exception:
                # Re-raise to let the service classify it
                raise

        # Use the injected service (preserves circuit breaker state)
        result, last_observed = self._verification_service.verify_condition(
            operation="write_verify",
            target=description,
            check=check,
            budget=budget,
        )

        if result == VerificationResult.SUCCESS:
            return

        # Build error message with last observed state
        detail = ""
        if last_observed:
            detail = f" last_state={last_observed}"

        from ...ports.verification import FailureType

        if result == VerificationResult.TIMED_OUT or api_error_occurred:
            # SYSTEMIC: Timeout or API error indicates infrastructure problem
            logger.warning("Write verification timed out (SYSTEMIC) for %s%s", description, detail)
            raise GitHubHttpError(
                f"Timed out verifying write: {description}",
                failure_type=FailureType.SYSTEMIC,
            )
        else:
            # ISSUE_LOCAL: Predicate returned false - write didn't take effect for this issue
            logger.warning("Write verification failed (ISSUE_LOCAL) for %s%s", description, detail)
            raise GitHubHttpError(
                f"Failed to verify write: {description}",
                failure_type=FailureType.ISSUE_LOCAL,
                issue_number=issue_number,
            )

    # IssueRepository implementation

    def _raw_issues_to_issues(self, raw_issues: list[dict]) -> "list[Issue]":
        """Convert raw API response to Issue objects."""
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

    def _retry_for_missing_ids(
        self,
        fetch_fn,
        required_stable_ids: set[str],
        issues: "list[Issue]",
    ) -> "list[Issue]":
        """Check for missing required IDs after a non-cached fetch.

        Does NOT block or sleep — the orchestrator tick loop handles retries
        naturally via the inflight ID mechanism. Blocking here would freeze
        the entire orchestrator (SSE serving, tick processing, everything).
        """
        found_ids = {i.key.stable_id() for i in issues}
        still_missing = required_stable_ids - found_ids
        if still_missing:
            logger.warning(
                "[INFLIGHT] Still missing %d required IDs after non-cached fetch: %s",
                len(still_missing), sorted(still_missing)
            )
        return issues

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
        required_stable_ids: set[str] | None = None,
    ) -> "list[Issue]":
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            milestone: Filter by milestone title.
            state: Filter by issue state ("open", "closed", or "all").
            limit: Maximum number of issues to return.
            required_stable_ids: Optional set of stable IDs that must be discovered.
                If provided and missing after cached fetch, retry without cache.

        Returns:
            List of GitHubIssue objects matching the criteria.

        Raises:
            GitHubHttpError: If GitHub rejects the query.
            GitHubTransportError: If the request fails before a response.
        """
        def _fetch(use_cache: bool) -> list[dict]:
            return self._client.list_issues(
                labels=labels,
                state=state,
                milestone=milestone,
                limit=limit,
                use_cache=use_cache,
            )

        # First attempt with cache
        raw_issues = _fetch(use_cache=True)
        issues = self._raw_issues_to_issues(raw_issues)

        # If required IDs are specified, check if any are missing
        if required_stable_ids:
            found_ids = {i.key.stable_id() for i in issues}
            missing = required_stable_ids - found_ids
            if missing:
                logger.info(
                    "[INFLIGHT] Missing %d required IDs after cached fetch, retrying without cache: %s",
                    len(missing), sorted(missing)
                )
                # Retry without cache to bypass potential stale 304
                raw_issues = _fetch(use_cache=False)
                issues = self._raw_issues_to_issues(raw_issues)

                # Retry with backoff for eventual consistency if still missing
                issues = self._retry_for_missing_ids(_fetch, required_stable_ids, issues)

                # Final check
                found_ids = {i.key.stable_id() for i in issues}
                if required_stable_ids <= found_ids:
                    logger.info("[INFLIGHT] All required IDs discovered")

        return issues

    def list_issues_delta(
        self,
        *,
        since: str,
        limit: int = 100,
    ) -> tuple["list[Issue]", str | None]:
        """List issues updated since watermark via repo-wide delta feed."""
        raw_issues, next_watermark = self._client.list_issues_since(
            since=since,
            state="all",
            limit=limit,
            use_cache=False,
        )
        return self._raw_issues_to_issues(raw_issues), next_watermark

    def search_issues_by_title(
        self,
        query_terms: list[str],
        *,
        limit: int = 30,
    ) -> "list[Issue]":
        """Targeted title search for resolver fallback.

        Wraps GitHub's `/search/issues` with `in:title is:issue`. Caller filters
        results — substring semantics mean "M9-006" can match
        "Notes on M9-006 (deprecated)", so the resolver post-checks
        parse_external_id equality.
        """
        raw = self._client.search_issues_by_title(query_terms, limit=limit)
        return self._raw_issues_to_issues(raw)

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
            if _is_not_found_error(e):
                return None
            logger.error("Failed to get issue %s: %s", issue_number, e)
            raise

    def get_issue_by_key(self, key: "IssueKey") -> "Issue | None":
        """Get an issue by its IssueKey.

        This is the reverse lookup: IssueKey -> Issue.
        For GitHubIssueKey, extracts the issue number and fetches.

        Args:
            key: The IssueKey to look up.

        Returns:
            The Issue if found, None otherwise.
        """
        from ...domain.issue_key import GitHubIssueKey

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
            List of label names. Returns empty list if the issue is not found
            or has no labels.
        """
        try:
            cached = self._get_cached_labels(issue_number)
            if cached is not None:
                return cached
            return self._get_issue_labels_cached(issue_number)
        except GitHubHttpError as e:
            if _is_not_found_error(e):
                return []
            logger.error(f"Failed to get labels for issue {issue_number}: {e}")
            raise

    def get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        """Get labels for a specific issue, bypassing adapter/ETag caches."""
        try:
            return self._get_issue_labels_fresh(issue_number)
        except GitHubHttpError as e:
            if _is_not_found_error(e):
                return []
            logger.error(f"Failed to get fresh labels for issue {issue_number}: {e}")
            raise

    def _get_cached_labels(self, issue_number: int) -> list[str] | None:
        """Get cached labels for an issue, or None if not cached/stale."""
        return self._adapter_cache.get_cached_labels(issue_number)

    def _get_issue_labels_cached(self, issue_number: int) -> list[str]:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ,
            issue_key=str(issue_number),
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            labels = self._get_issue_labels_with_retry(issue_number, use_cache=True)
        self.update_label_cache(issue_number, list(labels))
        return labels

    def _get_issue_labels_fresh(self, issue_number: int) -> list[str]:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ,
            issue_key=str(issue_number),
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            labels = self._get_issue_labels_with_retry(issue_number, use_cache=False)
        self.update_label_cache(issue_number, list(labels))
        return labels

    def _get_issue_labels_with_retry(self, issue_number: int, use_cache: bool) -> list[str]:
        import time

        for attempt in range(1, 4):
            try:
                return self._client.get_issue_labels(issue_number, use_cache=use_cache)
            except GitHubTransportError as e:
                if attempt >= 3:
                    raise
                logger.warning(
                    "Retrying label fetch for issue %s after error (%s): attempt %d/3",
                    issue_number,
                    e,
                    attempt,
                )
                time.sleep(0.5 * attempt)
        raise GitHubHttpError(f"Failed to fetch labels for issue {issue_number}")

    def _create_pr_with_retry(
        self,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool | None = None,
    ) -> dict[str, Any]:
        import time

        for attempt in range(1, 4):
            try:
                output = self._client.create_pr(title=title, body=body, head=head, base=base, draft=draft)
                if output is None:
                    raise GitHubHttpError("Failed to parse PR create response")
                return output
            except GitHubTransportError as e:
                if attempt >= 3:
                    raise
                logger.warning(
                    "Retrying PR create for branch %s after error (%s): attempt %d/3",
                    head,
                    e,
                    attempt,
                )
                time.sleep(1.0 * attempt)
        raise GitHubHttpError(f"Failed to create PR for branch {head}")

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
            for attempt in range(1, 4):
                try:
                    with gh_audit.context(
                        reason=gh_audit.AuditReason.GH_WRITE,
                        issue_key=str(issue_number),
                        scope=gh_audit.AuditScope.UNKNOWN,
                    ):
                        self._client.add_label(issue_number, label)
                    logger.debug(f"Added label '{label}' to issue {issue_number}")
                    break
                except GitHubTransportError as exc:
                    if attempt >= 3:
                        raise
                    logger.warning(
                        "Timeout adding label '%s' to issue %s (attempt %d/3): %s",
                        label,
                        issue_number,
                        attempt,
                        exc,
                    )
                    time.sleep(0.5 * attempt)
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                last_labels = self._get_issue_labels_fresh(issue_number)
                return label in last_labels

            self._verify_write(
                f"label add #{issue_number}:{label}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
                issue_number=issue_number,
            )
        except (GitHubHttpError, GitHubTransportError):
            logger.error(f"Failed to add label '{label}' to issue {issue_number}")
            raise
        finally:
            # Invalidate cache after write (per architecture: explicit invalidation)
            self.invalidate_label_cache(issue_number)

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue.

        Args:
            issue_number: The issue number to remove the label from.
            label: The label name to remove.

        Raises:
            GitHubHttpError: If the operation fails.
        """
        try:
            for attempt in range(1, 4):
                try:
                    with gh_audit.context(
                        reason=gh_audit.AuditReason.GH_WRITE,
                        issue_key=str(issue_number),
                        scope=gh_audit.AuditScope.UNKNOWN,
                    ):
                        self._client.remove_label(issue_number, label)
                    logger.debug(f"Removed label '{label}' from issue {issue_number}")
                    break
                except GitHubHttpError as exc:
                    if exc.status_code == 404:
                        # Label already absent is idempotent success for removal.
                        logger.info(
                            "Label '%s' not present on issue %s; treating remove as no-op",
                            label,
                            issue_number,
                        )
                        return
                    raise
                except GitHubTransportError as exc:
                    if attempt >= 3:
                        raise
                    logger.warning(
                        "Timeout removing label '%s' from issue %s (attempt %d/3): %s",
                        label,
                        issue_number,
                        attempt,
                        exc,
                    )
                    time.sleep(0.5 * attempt)
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                last_labels = self._get_issue_labels_fresh(issue_number)
                return label not in last_labels

            self._verify_write(
                f"label remove #{issue_number}:{label}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
                issue_number=issue_number,
            )
        except (GitHubHttpError, GitHubTransportError):
            logger.error(f"Failed to remove label '{label}' from issue {issue_number}")
            raise
        finally:
            # Invalidate cache after write (per architecture: explicit invalidation)
            self.invalidate_label_cache(issue_number)

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if an issue has a specific label.

        Args:
            issue_number: The issue number to check.
            label: The label name to check for.

        Returns:
            True if the issue has the label, False otherwise.
        """
        labels = self.get_issue_labels(issue_number)
        return label in labels

    # PRRepository implementation

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects.
        """
        # Check cache first
        cached_pr = self._adapter_cache.get_cached_pr_for_branch(branch, state)
        if cached_pr:
            return [cached_pr]
        # Fetch from API
        output = self._client.get_prs_for_branch(branch, state=state)
        if isinstance(output, list):
            prs = [self._pr_info_from_api(pr) for pr in output if isinstance(pr, dict)]
            for pr_info in prs:
                self._adapter_cache.cache_pr_info(pr_info)
            return prs
        return []

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Uses GraphQL to fetch PRs with head branch info in a single request,
        avoiding the N+1 individual get_pr() calls that the search API requires.

        Args:
            label: The label name to filter by.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects.
        """
        try:
            output = self._client.get_prs_with_label_graphql(label, state=state)
            return [pr for item in output if (pr := self._pr_info_from_api(item)) is not None]
        except (GitHubHttpError, GitHubTransportError):
            raise
        except Exception:
            logger.debug("GraphQL get_prs_with_label failed, falling back to search API", exc_info=True)
        # Fallback: search API + individual get_pr() calls
        output = self._client.get_prs_with_label(label, state=state)
        prs: list[PRInfo] = []
        for item in output:
            pr_info = self._fetch_pr_info_from_search(item)
            if pr_info:
                prs.append(pr_info)
        return prs

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get all pull requests associated with a specific issue.

        Finds PRs where:
        - Branch starts with the issue number followed by a dash (e.g., "328-feature-name")
        - OR title contains "#issue_number" (e.g., "#328: Feature")

        Args:
            issue_number: The issue number to find PRs for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects.
        """
        # Check cache first
        cached_pr = self._adapter_cache.get_cached_pr_for_issue(issue_number, state)
        if cached_pr:
            return [cached_pr]
        # Fetch from API
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
            self._adapter_cache.cache_pr_info(pr_info)
        return pr_infos

    def search_pr_refs_for_issue(self, issue_number: int) -> list[PRRef]:
        """Return lightweight PR refs for an issue from a single search.

        Unlike ``get_prs_for_issue``, this does NOT issue a ``GET /pulls/N`` per
        candidate — the orchestrator body marker lives in fields the search
        response already returns (``number``, ``html_url``, ``body``), so one
        search call answers "which PRs reference this issue" without hydration.
        """
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ,
            issue_key=str(issue_number),
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            items = self._client.get_prs_for_issue(issue_number)
        refs: list[PRRef] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_number = item.get("number")
            if not raw_number:
                continue
            try:
                number = int(raw_number)
            except (TypeError, ValueError):
                continue
            refs.append(
                PRRef(
                    number=number,
                    url=item.get("html_url") or item.get("url", "") or "",
                    title=item.get("title", "") or "",
                    body=item.get("body", "") or "",
                )
            )
        return refs

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        REST-only — does NOT populate ``status_check_rollup``. Callers
        that need check-status visibility must use
        ``get_pr_with_status_check_rollup`` (the awaiting-merge
        post-publish classifier is the sole consumer today).
        """
        try:
            output = self._client.get_pr(pr_number)
            if not isinstance(output, dict):
                return None
            return self._pr_info_from_api(output)
        except GitHubHttpError as e:
            if _is_not_found_error(e):
                return None
            logger.error("Failed to get PR %s: %s", pr_number, e)
            raise

    def get_pr_with_status_check_rollup(self, pr_number: int) -> PRInfo | None:
        """Get a PR augmented with the head-commit status-check rollup.

        Pays one extra GraphQL round-trip on top of the REST PR fetch.
        Used by the awaiting-merge reconciler to distinguish "merge
        state is unstable because checks are running" (wait) from
        "merge state is unstable because a check failed" (rework). A
        failed rollup fetch leaves rollup=None — the reconciler treats
        that as PENDING-equivalent, so we'll wait rather than rework
        on bad signal.
        """
        pr_info = self.get_pr(pr_number)
        if pr_info is None:
            return None
        try:
            rollup = self._client.get_pr_status_check_rollup(pr_number)
        except GitHubHttpError as e:
            logger.warning(
                "Failed to fetch status_check_rollup for PR %s: %s",
                pr_number, e,
            )
            rollup = None
        pr_info.status_check_rollup = _coerce_rollup_state(rollup)
        return pr_info

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List pull requests.

        Args:
            state: Filter by PR state ("open", "closed", "merged", or "all").
            limit: Maximum number of PRs to return.

        Returns:
            List of PRInfo objects.
        """
        output = self._client.list_prs(state=state, limit=limit)
        if isinstance(output, list):
            return [self._pr_info_from_api(pr) for pr in output if isinstance(pr, dict)]
        return []

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> PRInfo:
        """Create a new pull request, or return existing PR if one exists for the branch.

        This operation is idempotent - if a PR already exists for the given branch,
        it returns that PR instead of failing.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch).
            draft: Whether to create the PR as a draft.

        Returns:
            A PRInfo object representing the created or existing PR.

        Raises:
            GitHubHttpError: If there's an error creating the PR and no existing PR found.
        """
        # E2E dry-run mode: return fake PR without creating one
        # Used with E2E_DRY_RUN_PUSH to skip actual GitHub operations
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH")
        logger.debug("create_pr: E2E_DRY_RUN_PUSH=%r", dry_run)
        if dry_run == "1":
            import random
            fake_pr_number = random.randint(90000, 99999)
            logger.info(
                "[E2E_DRY_RUN] PR creation skipped (would create PR for head=%s base=%s)",
                head,
                base,
            )
            pr_info = PRInfo(
                number=fake_pr_number,
                title=title,
                url=f"https://github.com/{self.repo}/pull/{fake_pr_number}",
                branch=head,
                body=body,
                state="open",
                labels=[],
                draft=draft,
            )
            # Cache the dry-run PR so get_prs_for_issue() doesn't hit GitHub API
            self._adapter_cache.cache_pr_info(pr_info)
            return pr_info

        # First, check if a PR already exists for this branch
        existing_prs = self.get_prs_for_branch(head)
        if existing_prs:
            existing_pr = existing_prs[0]
            logger.info(f"PR already exists for branch {head}: #{existing_pr.number}")
            self._adapter_cache.cache_pr_info(existing_pr)
            return existing_pr

        try:
            logger.info("Creating PR via GitHub API: repo=%s head=%s base=%s", self.repo, head, base)
            output = self._create_pr_with_retry(title=title, body=body, head=head, base=base, draft=draft)
            if isinstance(output, dict):
                pr_info = self._pr_info_from_api(output)
                logger.info("Created PR #%s: %s", pr_info.number, pr_info.title)
                self._adapter_cache.cache_pr_info(pr_info)
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

    def set_pr_draft(self, pr_number: int, draft: bool) -> None:
        try:
            output = self._client.set_pr_draft(pr_number, draft)
            if isinstance(output, dict):
                pr_info = self._pr_info_from_api(output)
                self._adapter_cache.cache_pr_info(pr_info)
        except GitHubHttpError as e:
            logger.error("Failed to update PR #%s draft=%s: %s", pr_number, draft, e)
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
                issue_number=issue_or_pr_number,
            )

            return comment_url
        except GitHubHttpError:
            logger.error(f"Failed to add comment to issue/PR {issue_or_pr_number}")
            raise

    def _get_issue_for_repo(self, issue_number: int, target_repo: str) -> dict[str, Any] | None:
        """Get issue data for a specific repository.

        Creates a temporary client if needed for cross-repo lookups.

        Args:
            issue_number: The issue number to fetch.
            target_repo: Repository in owner/repo format.

        Returns:
            Issue data dict, or None if not found.
        """
        if target_repo != self.repo:
            temp_client = GitHubHttpClient(
                GitHubHttpConfig(
                    repo=target_repo,
                    token=self._client.config.token,
                    base_url=self._client.config.base_url,
                    timeout_seconds=self._client.config.timeout_seconds,
                )
            )
            try:
                return temp_client.get_issue(issue_number)
            finally:
                temp_client.close()
        else:
            return self._client.get_issue(issue_number)

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
            output = self._get_issue_for_repo(issue_number, target_repo)

            if isinstance(output, dict):
                return output.get("state")
            return None
        except GitHubHttpError as e:
            if _is_not_found_error(e):
                logger.debug("Issue %s in %s not found: %s", issue_number, target_repo, e)
                return None
            logger.debug("Error checking issue %s in %s: %s", issue_number, target_repo, e)
            raise
        except Exception as e:
            logger.debug("Error checking issue %s in %s: %s", issue_number, target_repo, e)
            raise

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the milestone name of an issue (or None if no milestone).

        This method implements the IssueStateChecker protocol for milestone validation.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo dependencies.
                  If None, uses this adapter's configured repo.

        Returns:
            The milestone name (title), or None if the issue has no milestone.
        """
        target_repo = repo or self.repo
        try:
            output = self._get_issue_for_repo(issue_number, target_repo)

            if isinstance(output, dict):
                milestone = output.get("milestone")
                if milestone and isinstance(milestone, dict):
                    return milestone.get("title")
            return None
        except GitHubHttpError as e:
            if _is_not_found_error(e):
                logger.debug("Issue %s in %s not found: %s", issue_number, target_repo, e)
                return None
            logger.debug("Error checking milestone for issue %s in %s: %s", issue_number, target_repo, e)
            raise
        except Exception as e:
            logger.debug("Error checking milestone for issue %s in %s: %s", issue_number, target_repo, e)
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
        from ...domain.issue_key import GitHubIssueKey, parse_external_id

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
        milestone: int | None = None,
    ) -> dict[str, Any] | None:
        """Create a new issue.

        Args:
            title: Issue title
            body: Issue body
            labels: Labels to add
            milestone: Milestone number to assign

        Returns:
            Full issue data dict including number and html_url, or None on failure
        """
        result = self._client.create_issue(
            title=title, body=body, labels=labels, milestone=milestone
        )
        if result is None:
            return None

        issue_number = result.get("number")
        if issue_number and labels:
            last_labels: list[str] = []

            def _check() -> bool:
                nonlocal last_labels
                try:
                    last_labels = self._get_issue_labels_fresh(issue_number)
                except GitHubHttpError as exc:
                    # A 404 on a freshly created issue is GitHub eventual consistency,
                    # not a permanent failure.  Return False so the verify loop retries.
                    if "404" in str(exc) or "not found" in str(exc).lower():
                        return False
                    raise
                return all(label in last_labels for label in labels)

            self._verify_write(
                f"issue create #{issue_number}",
                _check,
                detail_fn=lambda: {"labels": last_labels},
                issue_number=issue_number,
            )
        return result

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict[str, Any] | None:
        """Create a new milestone."""
        return self._client.create_milestone(
            title=title,
            description=description,
            due_on=due_on,
            state=state,
        )

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
            draft=pr.get("draft"),
            mergeable_state=(
                str(pr.get("mergeable_state")).lower()
                if pr.get("mergeable_state") is not None
                else None
            ),
        )

    def _fetch_pr_info_from_search(self, pr: dict[str, Any]) -> PRInfo | None:
        if not isinstance(pr, dict):
            return None
        pr_number = pr.get("number")
        if not pr_number:
            return None
        try:
            full = self._client.get_pr(pr_number)
        except GitHubHttpError as exc:
            if _is_not_found_error(exc):
                return None
            raise
        if not isinstance(full, dict):
            return None
        return self._pr_info_from_api(full)

    # -------------------- Additional Methods for Test Data/E2E --------------------

    def create_label(
        self,
        name: str,
        *,
        color: str = "ededed",
        description: str | None = None,
        force: bool = False,
    ) -> None:
        """Create a label in the repository.

        Args:
            name: Label name.
            color: Label color (hex without #).
            description: Optional label description.
            force: If True, update existing label if it already exists.
        """
        self._client.create_label(name, color=color, description=description, force=force)
        # Invalidate ETag cache so verification fetches fresh data
        self._client.invalidate_labels_etag()
        last_labels: list[str] = []

        def _check() -> bool:
            nonlocal last_labels
            labels = self._client.list_labels()
            last_labels = [l.get("name", "") for l in labels]
            return name in last_labels

        self._verify_write(
            f"label create {name}",
            _check,
            detail_fn=lambda: {"labels": last_labels},
        )

    def delete_label(self, name: str) -> None:
        """Delete a label from the repository.

        Args:
            name: Label name to delete.
        """
        self._client.delete_label(name)
        # Invalidate ETag cache so verification fetches fresh data
        self._client.invalidate_labels_etag()
        last_labels: list[str] = []

        def _check() -> bool:
            nonlocal last_labels
            labels = self._client.list_labels()
            last_labels = [l.get("name", "") for l in labels]
            return name not in last_labels

        self._verify_write(
            f"label delete {name}",
            _check,
            detail_fn=lambda: {"labels": last_labels},
        )

    def update_issue_state(self, issue_number: int, state: str) -> None:
        """Update an issue's state (open/closed).

        Args:
            issue_number: The issue number to update.
            state: New state ("open" or "closed").
        """
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_WRITE,
            issue_key=str(issue_number),
            scope=gh_audit.AuditScope.UNKNOWN,
        ):
            self._client.update_issue_state(issue_number, state)
        last_state: str | None = None

        def _check() -> bool:
            nonlocal last_state
            issue = self.get_issue(issue_number)
            last_state = issue.state if issue else None
            return last_state == state.lower()

        self._verify_write(
            f"issue state #{issue_number}:{state}",
            _check,
            detail_fn=lambda: {"state": last_state},
            issue_number=issue_number,
        )

    def list_branches(self) -> list[str]:
        """List all branches in the repository.

        Returns:
            List of branch names.
        """
        return self._client.list_branches()

    def delete_branch(self, branch: str) -> None:
        """Delete a branch from the repository.

        Args:
            branch: Branch name to delete.
        """
        self._client.delete_branch(branch)

        def _check() -> bool:
            return not self._client.branch_exists(branch)

        self._verify_write(
            f"branch delete {branch}",
            _check,
            detail_fn=lambda: {"branch": branch, "exists": self._client.branch_exists(branch)},
        )

    def branch_exists(self, branch: str) -> bool:
        """Check if a branch exists in the repository.

        Args:
            branch: Branch name to check.

        Returns:
            True if the branch exists, False otherwise.
        """
        return self._client.branch_exists(branch)

    def close_pr(self, pr_number: int) -> None:
        """Close a pull request.

        Args:
            pr_number: The PR number to close.
        """
        self._client.close_pr(pr_number)
        last_pr: PRInfo | None = None

        def _check() -> bool:
            nonlocal last_pr
            last_pr = self.get_pr(pr_number)
            return last_pr is not None and last_pr.state == "closed"

        self._verify_write(
            f"pr close #{pr_number}",
            _check,
            detail_fn=lambda: {"state": last_pr.state if last_pr else None},
        )

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        """Get comments on an issue.

        Args:
            issue_number: The issue number to get comments for.

        Returns:
            List of comment dictionaries.
        """
        return self._client.get_issue_comments(issue_number)

    def issue_comment_marker_present(self, issue_number: int, marker: str) -> bool:
        """Return True if any comment on the issue/PR contains ``marker``.

        Scans all comment pages (not just the first 100), so a marker comment
        posted beyond the first page is still detected. Used to dedupe
        orchestrator-authored marker comments before re-posting them.
        """
        return self._client.issue_comment_marker_present(issue_number, marker)

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get all reviews on a pull request.

        Args:
            pr_number: The PR number to get reviews for.

        Returns:
            List of review dicts with 'state', 'body', 'user' etc.
        """
        return self._client.get_pr_reviews(pr_number)

    def list_labels(self) -> list[dict[str, Any]]:
        """List all labels in the repository.

        Returns:
            List of label dictionaries with 'name', 'color', 'description' keys.
        """
        return self._client.list_labels()

    def list_all_labels(self) -> list[dict[str, Any]]:
        """List all labels with pagination (for cleanup operations).

        Returns:
            List of all label dictionaries across all pages.
        """
        return self._client.list_all_labels()

    def list_milestones(self, state: str = "open") -> list[dict[str, Any]]:
        """List milestones in the repository.

        Args:
            state: Filter by milestone state ('open', 'closed', 'all')

        Returns:
            List of milestone dictionaries with 'number', 'title', 'description', etc.
        """
        return self._client.list_milestones(state=state)

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> None:
        """Assign or clear a milestone on an issue."""
        result = self._client.update_issue_milestone(issue_number=issue_number, milestone=milestone)
        if result is None:
            return

        def _check() -> bool:
            issue = self.get_issue(issue_number)
            if issue is None:
                return False
            return issue.milestone_number == milestone

        self._verify_write(
            f"issue milestone #{issue_number}",
            _check,
            detail_fn=lambda: {"milestone": milestone},
            issue_number=issue_number,
        )
