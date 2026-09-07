"""Narrow Git capabilities for escrow and exact publication."""

from pathlib import Path
from typing import Protocol

from ..domain.exact_git import (
    ExactPushDestination,
    ExactPushResult,
    RefPinOutcome,
    RetainedRef,
)
from ..domain.validated_work_store import AncestryRelation


class ExactGit(Protocol):
    def read_pinned_ref(self, repository: Path, *, ref: str) -> RetainedRef | None: ...

    def pin_ref(self, repository: Path, *, ref: str, sha: str) -> RefPinOutcome: ...

    def verify_ref(self, repository: Path, *, ref: str, sha: str) -> bool: ...

    def delete_pinned_ref(self, repository: Path, *, ref: str, sha: str) -> None: ...

    def retained_refs(self, repository: Path) -> tuple[RetainedRef, ...]: ...

    def compare_commits(
        self, repository: Path, *, left: str, right: str
    ) -> AncestryRelation: ...

    def linked_worktrees(self, repository: Path) -> tuple[Path, ...]: ...

    def resolve_push_destination(
        self, repository: Path, *, remote: str
    ) -> ExactPushDestination: ...

    def push_exact(
        self,
        repository: Path,
        *,
        remote: str,
        branch: str,
        target_sha: str,
        expected_sha: str | None,
        destination: ExactPushDestination | None = None,
    ) -> ExactPushResult: ...
