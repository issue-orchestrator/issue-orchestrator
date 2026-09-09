"""Read-only revision identity and exact historical selection through Git."""

import logging
from pathlib import Path
from typing import Protocol
from ..ports.git import GitResult, GitError

logger = logging.getLogger(__name__)


class GitReadCommand(Protocol):
    def __call__(
        self, worktree: Path, args: list[str], *, check: bool = True
    ) -> GitResult: ...


class GitRevisionReader:
    def __init__(self, run: GitReadCommand) -> None:
        self._run_git = run

    def get_current_branch(self, worktree: Path) -> str | None:
        """Get the current branch name in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
            branch = result.stdout.strip()
            return None if branch == "HEAD" else branch  # HEAD means detached
        except GitError:
            logger.warning("Failed to get current branch in %s", worktree)
            return None

    def get_head_sha(self, worktree: Path) -> str | None:
        """Get the HEAD commit SHA in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "HEAD"])
            return result.stdout.strip()
        except GitError:
            logger.warning("Failed to get HEAD SHA in %s", worktree)
            return None

    def verify_historical_selection(
        self, repo_root: Path, branch_name: str, head_sha: str
    ) -> bool:
        from ..domain.validated_work import require_sha

        require_sha(head_sha)
        ref = "refs/heads/" + branch_name
        if (
            self._run_git(repo_root, ["check-ref-format", ref], check=False).returncode
            != 0
        ):
            return False
        commit = self._run_git(
            repo_root, ["rev-parse", "--verify", head_sha + "^{commit}"], check=False
        )
        if commit.returncode != 0 or commit.stdout.strip() != head_sha:
            return False
        return (
            self._run_git(
                repo_root, ["merge-base", "--is-ancestor", head_sha, ref], check=False
            ).returncode
            == 0
        )
