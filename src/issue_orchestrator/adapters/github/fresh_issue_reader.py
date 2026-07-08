"""GitHub adapter for fresh issue label reads."""

import logging

from ...infra import gh_audit
from ...infra.config import Config
from ...ports.fresh_issue_reader import FreshIssueReader
from .errors import GitHubHttpError
from .http_client import GitHubHttpClient, GitHubHttpConfig, build_github_auth
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

        auth_kwargs = config.github_auth_kwargs() if config else {}
        auth = build_github_auth(
            **auth_kwargs,
            repo=self.repo,
            api_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
            timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
        )
        self._client = GitHubHttpClient(
            GitHubHttpConfig(
                repo=self.repo,
                base_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
                timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
                auth=auth,
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
