from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class GitResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


class GitError(RuntimeError):
    def __init__(self, result: GitResult, message: str = "git command failed") -> None:
        super().__init__(
            f"{message}: rc={result.returncode} cmd={' '.join(result.argv)}\n"
            f"STDOUT:\n{result.stdout[:500]}\nSTDERR:\n{result.stderr[:500]}"
        )
        self.result = result


class Git(Protocol):
    """Tiny git wrapper interface."""

    def run(
        self,
        repo: Path,
        argv: list[str],
        *,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> GitResult:
        """Run a git command in repo context."""
        ...

    def status_porcelain(self, repo: Path) -> str: ...
    def current_branch(self, repo: Path) -> str: ...
    def head_sha(self, repo: Path) -> str: ...
    def branch_exists(self, repo: Path, branch: str) -> bool: ...
    def default_branch(self, repo: Path, remote: str = "origin") -> str: ...
    def fetch(self, repo: Path, remote: str = "origin", ref: str | None = None) -> None: ...
    def checkout_new_branch(self, repo: Path, branch: str, base_ref: str) -> None: ...
    def worktree_add(self, repo: Path, path: Path, branch: str) -> None: ...
    def worktree_remove(self, repo: Path, path: Path, force: bool = True, prune: bool = True) -> None: ...
    def commit(self, repo: Path, message: str) -> None: ...
    def push(
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        set_upstream: bool = True,
        force_with_lease: bool = False,
        skip_hooks: bool = False,
    ) -> None: ...
