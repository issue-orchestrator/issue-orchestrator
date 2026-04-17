"""Shared Git command helpers for worktree adapter modules."""

from pathlib import Path

from ...ports.git import GitResult
from ..git.git_cli import GitCLI, SubprocessCommandRunner

_git = GitCLI(runner=SubprocessCommandRunner())

__all__ = ["_git", "_git_env_no_prompt", "_git_run"]


def _git_run(
    repo: Path,
    argv: list[str],
    *,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> GitResult:
    return _git.run(repo=repo, argv=argv, check=check, env=env)


def _git_env_no_prompt() -> dict[str, str]:
    env = _git.clean_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env
