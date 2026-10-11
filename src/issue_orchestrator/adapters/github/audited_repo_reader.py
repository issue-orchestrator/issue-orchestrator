"""GitHub reads of the audited repository for the improver's toolbox (#8001).

Serves only what :class:`~...domain.improver_toolbox_policy.AuditedRepoReadPolicy`
allowed. The credential stays in this process: the agent asks the toolbox,
and only the answer reaches it.
"""

from __future__ import annotations

from typing import Any

from ...domain.improver_toolbox_policy import GitHubRead
from ...infra import gh_audit
from ...ports.improver_toolbox import AuditedReadTooLarge
from .errors import GitHubResponseTooLarge
from .http_client import GitHubHttpClient, GitHubHttpConfig, build_github_auth

API_URL = "https://api.github.com"


class GitHubAuditedRepoReader:
    def __init__(self, repo: str, *, http_client: GitHubHttpClient | None = None) -> None:
        self._client = http_client or GitHubHttpClient(
            GitHubHttpConfig(repo=repo, base_url=API_URL, auth=build_github_auth(repo=repo, api_url=API_URL))
        )

    def get(self, read: GitHubRead, *, max_bytes: int) -> Any:
        with gh_audit.context(
            reason=gh_audit.AuditReason.GH_READ, issue_key=None, scope=gh_audit.AuditScope.ON_DEMAND
        ):
            try:
                return self._client.get_json_bounded(
                    read.path, params=dict(read.params), max_bytes=max_bytes, caller="improver_toolbox"
                )
            except GitHubResponseTooLarge as too_large:
                raise AuditedReadTooLarge(str(too_large)) from too_large


__all__ = ["GitHubAuditedRepoReader"]
