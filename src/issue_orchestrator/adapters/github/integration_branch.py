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
    MergedIntoBranchListing,
    OpenPullRequestRef,
)
from ...ports.pull_request_tracker import StatusCheckRollupRead
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

    def merge_head_onto(self, branch: str, *, tip_sha: str, head_sha: str, message: str) -> str:
        if not self.compare_commits(tip_sha, head_sha).contains_base:
            # The head's tree is the merge result only when the head contains the tip.
            raise GitHubHttpError(f"{head_sha[:12]} does not contain {branch} at {tip_sha[:12]}; not merged")
        tree = (self._client.get_git_commit(head_sha).get("tree") or {}).get("sha")
        commit = self._client.create_git_commit(
            message=message, tree_sha=_sha(tree, what="head commit tree"), parents=[tip_sha, head_sha],
        )
        merged = _sha(commit.get("sha"), what="merge commit")
        # force=false: GitHub refuses unless the merge commit descends from the
        # branch's CURRENT head. That admits exactly one race besides "still at
        # tip_sha": the branch fast-forwarded to a commit the head already
        # contains. Then the tree is still the head's own tree (the head
        # contains the new tip too) and the checks that passed on the head
        # still cover it, so the merge stays correct (#8144 review r3 F3).
        self._client.update_git_ref(ref=f"refs/heads/{branch}", sha=merged, force=False)
        return merged

    def read_commit_check_rollup(self, sha: str) -> StatusCheckRollupRead:
        rollup = self._client.get_commit_check_rollup(sha)
        if rollup.capability != "ok":
            # An unread source may hide a failed check: never a green answer.
            denied = rollup.capability == "permission_denied"
            return StatusCheckRollupRead(state=None, capability=rollup.capability, primary_source_denied=denied)
        state = rollup.state
        if state not in (None, "SUCCESS", "FAILURE", "PENDING", "EXPECTED", "ERROR"):
            raise GitHubHttpError(f"GitHub reported an unknown check state {state!r} for {sha[:12]}")
        return StatusCheckRollupRead(state=state, capability="ok")

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

    def open_pull_request(self, *, head: str, base: str, title: str, body: str) -> OpenPullRequestRef:
        # Straight to GitHub's create: the adapter's create_pr reuses ANY open
        # PR from the head branch, whatever its base (#8144 review r7 F2).
        payload = self._client.create_pr(title, body, head, base)
        if payload is None or (payload.get("base") or {}).get("ref") != base:
            raise GitHubHttpError(f"GitHub did not open a PR from {head} into {base}")
        number, url = payload.get("number"), payload.get("html_url")
        if type(number) is not int or not isinstance(url, str):
            raise GitHubHttpError("GitHub create-PR payload has no number or html_url")
        return OpenPullRequestRef(number=number, url=url, body=body)

    def update_pull_request_body(self, pr_number: int, body: str) -> None:
        self._client.update_pr_body(pr_number, body)

    def merged_pull_requests_into(self, base: str) -> MergedIntoBranchListing:
        merged: list[MergedIntoBranch] = []
        for page in range(1, MERGED_PULLS_PAGE_CAP + 1):
            pulls = self._client.list_pulls(
                state="closed", base=base, sort="updated", direction="desc", page=page,
            )
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
            if len(pulls) < 100:
                return MergedIntoBranchListing(pulls=tuple(merged), complete=True)
        return MergedIntoBranchListing(pulls=tuple(merged), complete=False)


#: Pages of 100 closed PRs the delivery listing reads at most (1000 PRs).
MERGED_PULLS_PAGE_CAP = 10

__all__ = ["GitHubIntegrationBranchMixin", "MERGED_PULLS_PAGE_CAP"]
