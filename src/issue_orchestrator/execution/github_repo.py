"""Helpers for resolving GitHub repository info from git remotes."""

from __future__ import annotations

import subprocess


class GitRepoError(RuntimeError):
    """Raised when repository resolution fails."""


def get_repo_from_git() -> str:
    result = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
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
