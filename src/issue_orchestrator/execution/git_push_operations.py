"""Git push helpers for transport auth, fetch, and error classification."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from ..adapters.git.git_cli import GIT_ENV_STRIP
from ..ports.git import GitResult
from ..ports.working_copy import PreflightResult, PushResult


class GitAuthEnvProvider(Protocol):
    """Supplies per-command git auth environment overrides."""

    def git_env_overrides(self, *, remote: str) -> dict[str, str] | None:
        """Return git env overrides, or None for ambient git credentials."""
        ...


class GitCommand(Protocol):
    """Callable shape for running git commands from a working copy."""

    def __call__(
        self,
        worktree: Path,
        args: list[str],
        *,
        check: bool = True,
        timeout_s: int | None = None,
        env: dict[str, str] | None = None,
    ) -> GitResult:
        """Run a git command."""
        ...


class ClearStaleRemoteRef(Protocol):
    """Callable shape for clearing a stale remote-tracking ref."""

    def __call__(self, worktree: Path, remote: str, branch: str) -> None:
        """Clear a stale remote ref."""
        ...


def prepare_git_auth_env(
    git_auth: GitAuthEnvProvider | None,
    *,
    remote: str,
) -> dict[str, str] | None:
    """Prepare a clean git process environment with optional auth overrides."""
    if git_auth is None:
        return None
    overrides = git_auth.git_env_overrides(remote=remote)
    if not overrides:
        return None
    env = dict(os.environ)
    for var in GIT_ENV_STRIP:
        env.pop(var, None)
    env.update(overrides)
    return env


def push_auth_env_or_failure(
    git_auth: GitAuthEnvProvider | None,
    *,
    remote: str,
    branch: str,
) -> tuple[dict[str, str] | None, PushResult | None]:
    """Prepare push auth env, converting auth setup failures to PushResult."""
    try:
        return prepare_git_auth_env(git_auth, remote=remote), None
    except Exception as exc:
        return None, PushResult(
            success=False,
            branch=branch,
            remote=remote,
            message=f"Failed to prepare git authentication: {exc}",
            retryable=False,
        )


def preflight_auth_env_or_failure(
    git_auth: GitAuthEnvProvider | None,
    *,
    remote: str,
) -> tuple[dict[str, str] | None, PreflightResult | None]:
    """Prepare preflight auth env, converting auth setup failures to result."""
    try:
        return prepare_git_auth_env(git_auth, remote=remote), None
    except Exception as exc:
        return None, PreflightResult(
            would_succeed=False,
            error=f"Failed to prepare git authentication: {exc}",
            fix_hint="Check GitHub authentication configuration.",
        )


def fetch_for_push(
    run_git: GitCommand,
    clear_stale_remote_ref: ClearStaleRemoteRef,
    worktree: Path,
    remote: str,
    branch: str,
    *,
    env: dict[str, str] | None = None,
) -> PushResult | None:
    """Fetch remote refs before push, return error result if fetch fails."""
    try:
        fetch_result = run_git(
            worktree,
            ["fetch", remote, branch],
            check=False,
            timeout_s=60,
            env=env,
        )
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr or ""
            if "couldn't find remote ref" not in stderr:
                return PushResult(
                    success=False,
                    branch=branch,
                    remote=remote,
                    message=f"Failed to update tracking refs before push: {stderr}",
                    retryable=True,
                )
            clear_stale_remote_ref(worktree, remote, branch)
    except Exception as exc:
        error_str = str(exc)
        if "couldn't find remote ref" in error_str:
            clear_stale_remote_ref(worktree, remote, branch)
        else:
            return PushResult(
                success=False,
                branch=branch,
                remote=remote,
                message=f"Failed to update tracking refs before push: {exc}",
                retryable=True,
            )
    return None


def build_push_args(
    remote: str,
    branch: str,
    set_upstream: bool,
    skip_hooks: bool,
) -> list[str]:
    """Build git push command arguments."""
    args = ["push", "--force-with-lease"]
    if skip_hooks:
        args.append("--no-verify")
    if set_upstream:
        args.extend(["-u", remote, branch])
    else:
        args.append(remote)
    return args


def determine_retryable(error_msg: str) -> bool:
    """Determine if a push error is retryable."""
    if "non-fast-forward" in error_msg or "rejected" in error_msg:
        return False
    if "permission denied" in error_msg.lower():
        return False
    return True


def fetch_for_preflight(
    run_git: GitCommand,
    clear_stale_remote_ref: ClearStaleRemoteRef,
    worktree: Path,
    remote: str,
    branch: str,
    *,
    env: dict[str, str] | None = None,
) -> PreflightResult | None:
    """Fetch remote refs before preflight check, return error result if fetch fails."""
    try:
        fetch_result = run_git(
            worktree,
            ["fetch", remote, branch],
            check=False,
            timeout_s=60,
            env=env,
        )
        if fetch_result.returncode != 0:
            stderr = fetch_result.stderr or ""
            if "couldn't find remote ref" not in stderr:
                return PreflightResult(
                    would_succeed=False,
                    error=f"Failed to update tracking refs: {stderr}",
                    fix_hint="Network or remote issue - retry later",
                )
            clear_stale_remote_ref(worktree, remote, branch)
    except Exception as exc:
        error_str = str(exc)
        if "couldn't find remote ref" not in error_str:
            return PreflightResult(
                would_succeed=False,
                error=f"Failed to update tracking refs: {exc}",
                fix_hint="Network or remote issue - retry later",
            )
        clear_stale_remote_ref(worktree, remote, branch)
    return None


def get_preflight_fix_hint(error_msg: str) -> str | None:
    """Determine fix hint based on preflight error message."""
    if "stale info" in error_msg or "rejected" in error_msg:
        return "Branch has diverged. Run: git fetch origin && git rebase origin/main"
    if "no upstream" in error_msg.lower():
        return "No upstream branch set. This should be handled automatically."
    if "permission denied" in error_msg.lower() or "authentication" in error_msg.lower():
        return "Authentication issue - contact orchestrator administrator."
    return None
