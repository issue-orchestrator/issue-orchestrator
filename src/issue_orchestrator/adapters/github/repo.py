"""Helpers for resolving GitHub repository info from git remotes."""

from __future__ import annotations

from pathlib import Path

from ..git.git_cli import GitCLI, SubprocessCommandRunner


class GitRepoError(RuntimeError):
    """Raised when repository resolution fails."""


def get_repo_from_git() -> str:
    git = GitCLI(runner=SubprocessCommandRunner())
    result = git.run(
        repo=Path("."),
        argv=["config", "--get", "remote.origin.url"],
        check=False,
    )
    if result.returncode != 0:
        raise GitRepoError("Could not determine repository from git remote")

    remote_url = result.stdout.strip()
    if remote_url.startswith("https://github.com/"):
        repo = remote_url.replace("https://github.com/", "").replace(".git", "")
    elif remote_url.startswith("git@github.com:"):
        repo = remote_url.replace("git@github.com:", "").replace(".git", "")
    else:
        raise GitRepoError(f"Unrecognized GitHub remote URL: {remote_url}")
    return repo
