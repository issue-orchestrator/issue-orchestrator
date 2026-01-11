"""Execution-layer helpers for running git commands via the GitCLI adapter."""

from __future__ import annotations

from pathlib import Path

from ..adapters.git.git_cli import GitCLI, SubprocessCommandRunner
from ..ports.git import GitError


def run_git(args: list[str], cwd: Path | None = None, *, timeout_s: int = 10) -> tuple[bool, str]:
    """Run git command, return (success, output)."""
    git = GitCLI(runner=SubprocessCommandRunner())
    repo = cwd or Path.cwd()
    try:
        result = git.run(repo, args, timeout_s=timeout_s, check=False)
    except GitError:
        return False, ""
    return result.returncode == 0, result.stdout.strip()
