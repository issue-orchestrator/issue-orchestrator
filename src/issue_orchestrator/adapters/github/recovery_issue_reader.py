"""Uncached complete issue observations for retained-work publication."""

from ...domain.recovery_entry import RecoveryIssue, RecoveryIssueState
from ...ports.recovery_issue_reader import RecoveryIssueReadError
from ...ports.repository_host import RepositoryHostError
from .http_client import GitHubHttpClient


class GitHubRecoveryIssueReader:
    def __init__(self, client: GitHubHttpClient, *, repo_slug: str) -> None:
        if client.config.repo != repo_slug:
            raise ValueError("HTTP client repository differs from recovery repository")
        self._client, self._repo = client, repo_slug

    def read(self, repo_slug: str, issue_number: int) -> RecoveryIssue:
        if repo_slug != self._repo:
            raise RecoveryIssueReadError("recovery issue belongs to another repository")
        try:
            raw = self._client.get_issue(issue_number, use_cache=False)
            if raw is None or "pull_request" in raw or type(raw["labels"]) is not list:
                raise ValueError("missing or malformed recovery issue")
            observed = RecoveryIssue(repo_slug, raw["number"], raw["title"],
                RecoveryIssueState(raw["state"]), tuple(label["name"] for label in raw["labels"]))
            observed.require_identity(repo_slug, issue_number)
            return observed
        except (RepositoryHostError, KeyError, TypeError, ValueError) as error:
            raise RecoveryIssueReadError(f"cannot read recovery issue #{issue_number}: {error}") from error
