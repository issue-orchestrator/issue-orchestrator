"""Store ancestry adapter: exact pinned commits in one durable repository."""

from pathlib import Path

from ..domain.validated_work_store import AncestryRelation, CommitReference
from ..ports.exact_git import ExactGit


class GitValidatedWorkAncestry:
    def __init__(self, *, repository: Path, repo_slug: str, git: ExactGit) -> None:
        self._repository = repository
        self._repo_slug = repo_slug
        self._git = git

    def _reachable(self, reference: CommitReference) -> bool:
        if reference.key.repo_slug != self._repo_slug:
            return False
        # Empty pin is the store's explicit published/baseline object reference.
        if not reference.pinned_ref:
            relation = self._git.compare_commits(
                self._repository,
                left=reference.key.validated_head_sha,
                right=reference.key.validated_head_sha,
            )
            return relation is AncestryRelation.EQUAL
        return self._git.verify_ref(
            self._repository,
            ref=reference.pinned_ref,
            sha=reference.key.validated_head_sha,
        )

    def compare(
        self, left: CommitReference, right: CommitReference
    ) -> AncestryRelation:
        a, b = self._reachable(left), self._reachable(right)
        if not a or not b:
            return (
                AncestryRelation.BOTH_UNREACHABLE
                if not a and not b
                else AncestryRelation.LEFT_UNREACHABLE
                if not a
                else AncestryRelation.RIGHT_UNREACHABLE
            )
        return self._git.compare_commits(
            self._repository,
            left=left.key.validated_head_sha,
            right=right.key.validated_head_sha,
        )
