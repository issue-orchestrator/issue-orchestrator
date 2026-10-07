"""GitHub reads of the audited repository for the improver's toolbox (#8001).

Serves only what :class:`~...domain.improver_toolbox_policy.AuditedRepoReadPolicy`
allowed. The credential stays in this process: the agent asks the toolbox,
and only the answer reaches it.
"""

from __future__ import annotations

from typing import Any

from ...domain.improver_toolbox_policy import GitHubRead
from ...infra import gh_audit
from .http_client import GitHubHttpClient, GitHubHttpConfig, build_github_auth

API_URL = "https://api.github.com"


class GitHubAuditedRepoReader:
    def __init__(self, repo: str, *, http_client: GitHubHttpClient | None = None) -> None:
        self._client = http_client or GitHubHttpClient(
            GitHubHttpConfig(repo=repo, base_url=API_URL, auth=build_github_auth(repo=repo, api_url=API_URL))
        )

    def get(self, read: GitHubRead) -> Any:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ, issue_key=None, scope=gh_audit.AuditScope.ON_DEMAND
        ):
            return self._client.get_json(read.path, params=dict(read.params), caller="improver_toolbox")


__all__ = ["GitHubAuditedRepoReader"]
