"""GitHub adapter for fresh issue label reads."""

import logging

from ...infra import gh_audit
from ...infra.config import Config
from ...ports.fresh_issue_reader import FreshIssueReader
from .http_client import GitHubHttpClient, GitHubHttpConfig, GitHubHttpError, resolve_github_token
from .repo import get_repo_from_git, GitRepoError

logger = logging.getLogger(__name__)


class GitHubFreshIssueReader(FreshIssueReader):
    """FreshIssueReader implementation for GitHub."""

    def __init__(self, repo: str | None = None, config: Config | None = None) -> None:
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

    def read_issue_labels(self, issue_number: int) -> list[str]:
        try:
            with gh_audit.context(
                reason=gh_audit.AuditReason.GH_READ,
                issue_key=str(issue_number),
                scope=gh_audit.AuditScope.UNKNOWN,
            ):
                return self._client.get_issue_labels(issue_number, use_cache=False)
        except GitHubHttpError as exc:
            logger.error("Failed to read fresh labels for issue %s: %s", issue_number, exc)
            return []
        except Exception as exc:
            logger.error("Unexpected error reading fresh labels for issue %s: %s", issue_number, exc)
            return []
