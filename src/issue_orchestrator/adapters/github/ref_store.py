"""Reusable GitHub Git-ref compare-and-swap storage.

One logical record lives in one ref.  The ref points at a commit whose message
is the record and whose parent is the previously observed commit.  GitHub's
non-force fast-forward check is therefore the compare-and-swap boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import GitHubHttpError

if TYPE_CHECKING:
    from .http_client import GitHubHttpClient


@dataclass(frozen=True)
class GitRefSnapshot:
    """One exact ref value and the commit-message record it carries."""

    ref: str
    commit_sha: str
    tree_sha: str
    message: str


class GitRefCasStore:
    """Opaque one-record-per-ref storage with atomic create and update."""

    def __init__(self, client: "GitHubHttpClient", *, ref_prefix: str) -> None:
        self._client = client
        self._ref_prefix = ref_prefix.rstrip("/")
        self._default_branch: str | None = None

    def ref_for(self, key: str) -> str:
        return f"{self._ref_prefix}/{key}"

    def read(self, key: str) -> GitRefSnapshot | None:
        ref = self.ref_for(key)
        ref_payload = self._client.get_git_ref(ref)
        if ref_payload is None:
            return None
        commit_sha = payload_commit_sha(ref_payload)
        commit_payload = self._client.get_git_commit(commit_sha)
        return GitRefSnapshot(
            ref=ref,
            commit_sha=commit_sha,
            tree_sha=payload_tree_sha(commit_payload),
            message=str(commit_payload.get("message") or ""),
        )

    def create(self, key: str, message: str) -> bool:
        default_branch = self._default_branch_name()
        base_ref = self._client.get_git_ref(f"refs/heads/{default_branch}")
        if base_ref is None:
            raise ValueError(
                f"default branch ref refs/heads/{default_branch} was not found"
            )
        base_sha = payload_commit_sha(base_ref)
        base_commit = self._client.get_git_commit(base_sha)
        commit = self._client.create_git_commit(
            message=message,
            tree_sha=payload_tree_sha(base_commit),
            parents=[base_sha],
        )
        try:
            self._client.create_git_ref(ref=self.ref_for(key), sha=payload_sha(commit))
            return True
        except GitHubHttpError as exc:
            if is_ref_conflict(exc):
                return False
            raise

    def update(self, snapshot: GitRefSnapshot, message: str) -> bool:
        commit = self._client.create_git_commit(
            message=message,
            tree_sha=snapshot.tree_sha,
            parents=[snapshot.commit_sha],
        )
        try:
            self._client.update_git_ref(
                ref=snapshot.ref, sha=payload_sha(commit), force=False
            )
            return True
        except GitHubHttpError as exc:
            if is_ref_conflict(exc):
                return False
            raise

    def delete(self, snapshot: GitRefSnapshot) -> bool:
        try:
            self._client.delete_git_ref(snapshot.ref)
            return True
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return True
            raise

    def _default_branch_name(self) -> str:
        if self._default_branch is None:
            self._default_branch = self._client.get_default_branch()
        return self._default_branch


def payload_sha(payload: dict) -> str:
    sha = payload.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub payload missing sha: {payload}")
    return sha


def payload_commit_sha(payload: dict) -> str:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise ValueError(f"GitHub ref payload missing object: {payload}")
    sha = obj.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub ref payload missing object.sha: {payload}")
    return sha


def payload_tree_sha(payload: dict) -> str:
    tree = payload.get("tree")
    if not isinstance(tree, dict):
        raise ValueError(f"GitHub commit payload missing tree: {payload}")
    sha = tree.get("sha")
    if not isinstance(sha, str) or not sha:
        raise ValueError(f"GitHub commit payload missing tree.sha: {payload}")
    return sha


def is_ref_conflict(exc: GitHubHttpError) -> bool:
    return exc.status_code in {409, 422}
