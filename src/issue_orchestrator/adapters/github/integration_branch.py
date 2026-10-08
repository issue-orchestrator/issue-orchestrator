"""GitHub implementation of the integration-branch port (#8144).

A mixin of :class:`~.github_adapter.GitHubAdapter`: it turns the REST payloads
of :class:`~.http_client.GitHubHttpClient` into the domain types of
:mod:`~...domain.integration_branch`. Every HTTP failure propagates as the
client's :class:`~.errors.GitHubHttpError` (a ``RepositoryHostError``); a
payload missing what the port promises raises one too. Nothing defaults.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...domain.integration_branch import (
    BranchComparison,
    BranchMergeOutcome,
    MergedIntoBranch,
    OpenPullRequestRef,
)
from .errors import GitHubHttpError

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient


def _sha(value: object, *, what: str) -> str:
    if not isinstance(value, str) or len(value) != 40:
        raise GitHubHttpError(f"GitHub {what} payload has no commit SHA")
    return value


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise GitHubHttpError(f"GitHub compare payload has no {key}")
    return value


class GitHubIntegrationBranchMixin:
    """The :class:`~...ports.integration_branch.IntegrationBranchHost` methods."""

    _client: "GitHubHttpClient"

    def branch_head(self, branch: str) -> str | None:
        payload = self._client.get_git_ref(f"refs/heads/{branch}")
        if payload is None:
            return None
        return _sha((payload.get("object") or {}).get("sha"), what="branch ref")

    def create_branch(self, branch: str, sha: str) -> None:
        self._client.create_git_ref(ref=f"refs/heads/{branch}", sha=sha)

    def fast_forward_branch(self, branch: str, sha: str) -> None:
        self._client.update_git_ref(ref=f"refs/heads/{branch}", sha=sha, force=False)

    def compare_commits(self, base: str, head: str) -> BranchComparison:
        payload = self._client.compare_commits(base, head)
        ahead_by = _count(payload, "ahead_by")
        commits = payload.get("commits")
        if not isinstance(commits, list):
            raise GitHubHttpError("GitHub compare payload has no commits list")
        shas = tuple(_sha(commit.get("sha") if isinstance(commit, dict) else None, what="compare commit")
                     for commit in commits)
        total = payload.get("total_commits", ahead_by)
        if type(total) is not int:
            raise GitHubHttpError("GitHub compare payload has a malformed total_commits")
        return BranchComparison(
            ahead_by=ahead_by,
            behind_by=_count(payload, "behind_by"),
            commit_shas=shas,
            complete=len(shas) >= total,
        )

    def merge_branch(self, *, base: str, head: str, message: str) -> BranchMergeOutcome:
        try:
            payload = self._client.merge_branch(base=base, head=head, message=message)
        except GitHubHttpError as exc:
            if exc.status_code == 409:
                return BranchMergeOutcome.CONFLICT
            raise
        if not payload:  # 204: base already contains head
            return BranchMergeOutcome.UP_TO_DATE
        _sha(payload.get("sha"), what="merge")
        return BranchMergeOutcome.MERGED

    def update_pull_request_branch(self, pr_number: int, *, expected_head_sha: str) -> None:
        self._client.update_pull_request_branch(pr_number, expected_head_sha=expected_head_sha)

    def merge_pull_request(
        self, pr_number: int, *, head_sha: str, method: str, title: str, message: str
    ) -> str:
        payload = self._client.merge_pull_request(
            pr_number, head_sha=head_sha, method=method, title=title, message=message
        )
        if payload.get("merged") is not True:
            raise GitHubHttpError(f"GitHub did not merge PR #{pr_number}: {payload.get('message')!r}")
        return _sha(payload.get("sha"), what="PR merge")

    def find_open_pull_request(self, *, head: str, base: str) -> OpenPullRequestRef | None:
        pulls = self._client.list_pulls(state="open", base=base, head=head)
        if not pulls:
            return None
        if len(pulls) > 1:
            numbers = sorted(int(pr.get("number", 0)) for pr in pulls)
            raise GitHubHttpError(f"several open PRs from {head} into {base}: {numbers}")
        pr = pulls[0]
        number, url = pr.get("number"), pr.get("html_url")
        if type(number) is not int or not isinstance(url, str):
            raise GitHubHttpError("GitHub pull payload has no number or html_url")
        return OpenPullRequestRef(number=number, url=url, body=pr.get("body") or "")

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        self._client.update_pr_body(pr_number, body)

    def merged_pull_requests_into(self, base: str) -> tuple[MergedIntoBranch, ...]:
        pulls = self._client.list_pulls(state="closed", base=base, sort="updated", direction="desc")
        merged: list[MergedIntoBranch] = []
        for pr in pulls:
            if not pr.get("merged_at"):
                continue
            number, url, title = pr.get("number"), pr.get("html_url"), pr.get("title")
            if type(number) is not int or not isinstance(url, str) or not isinstance(title, str):
                raise GitHubHttpError("GitHub pull payload has no number, html_url or title")
            merged.append(MergedIntoBranch(
                number=number, title=title, url=url,
                merge_commit_sha=_sha(pr.get("merge_commit_sha"), what="merged pull"),
            ))
        return tuple(merged)


__all__ = ["GitHubIntegrationBranchMixin"]
