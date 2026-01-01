"""Git working copy adapter for local VCS operations.

This adapter implements the WorkingCopy protocol for git operations.
It handles local worktree operations: push, rebase, commit info, etc.

Part of the execution layer - performs actions, does not make decisions.
"""

import logging
import re
import subprocess
import time
from pathlib import Path

from ..ports.working_copy import (
    WorkingCopy,
    CommitInfo,
    BranchStatus,
    PushResult,
    RebaseResult,
)

logger = logging.getLogger(__name__)


class GitWorkingCopy:
    """Git implementation of the WorkingCopy protocol.

    Performs local git operations in worktree directories.
    This is execution-layer code - it does what it's told,
    without making policy decisions.
    """

    def _run_git(
        self,
        worktree: Path,
        args: list[str],
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command in the worktree context.

        Args:
            worktree: Path to the worktree directory.
            args: Git command arguments (without 'git').
            check: Whether to raise on non-zero exit.
            capture_output: Whether to capture stdout/stderr.

        Returns:
            CompletedProcess with results.
        """
        cmd = ["git", "-C", str(worktree)] + args
        logger.debug("Running: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            check=check,
            capture_output=capture_output,
            text=True,
        )

    def get_current_branch(self, worktree: Path) -> str | None:
        """Get the current branch name in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
            branch = result.stdout.strip()
            return None if branch == "HEAD" else branch  # HEAD means detached
        except subprocess.CalledProcessError:
            logger.warning("Failed to get current branch in %s", worktree)
            return None

    def get_head_sha(self, worktree: Path) -> str | None:
        """Get the HEAD commit SHA in the worktree."""
        try:
            result = self._run_git(worktree, ["rev-parse", "HEAD"])
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            logger.warning("Failed to get HEAD SHA in %s", worktree)
            return None

    def get_branch_status(self, worktree: Path) -> BranchStatus | None:
        """Get the status of the current branch."""
        branch = self.get_current_branch(worktree)
        if not branch:
            return None

        try:
            # Check for uncommitted changes
            status_result = self._run_git(worktree, ["status", "--porcelain"])
            clean = len(status_result.stdout.strip()) == 0

            # Check ahead/behind
            ahead = 0
            behind = 0
            has_remote = False

            try:
                # Get upstream tracking info
                upstream_result = self._run_git(
                    worktree,
                    ["rev-list", "--left-right", "--count", f"HEAD...@{{u}}"],
                )
                parts = upstream_result.stdout.strip().split()
                if len(parts) == 2:
                    ahead = int(parts[0])
                    behind = int(parts[1])
                    has_remote = True
            except subprocess.CalledProcessError:
                # No upstream tracking
                has_remote = False

            return BranchStatus(
                branch=branch,
                ahead=ahead,
                behind=behind,
                has_remote=has_remote,
                clean=clean,
            )
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to get branch status: %s", e)
            return None

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        """Check if there are uncommitted changes in the worktree."""
        try:
            result = self._run_git(worktree, ["status", "--porcelain"])
            return len(result.stdout.strip()) > 0
        except subprocess.CalledProcessError:
            logger.warning("Failed to check uncommitted changes in %s", worktree)
            return True  # Assume dirty on error (safer)

    def get_commits_ahead_of_main(self, worktree: Path) -> list[CommitInfo]:
        """Get commits that are ahead of main branch."""
        try:
            # Get commits in HEAD but not in origin/main
            result = self._run_git(
                worktree,
                [
                    "log",
                    "origin/main..HEAD",
                    "--format=%H|%s|%an|%h",
                ],
            )

            commits = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commits.append(
                        CommitInfo(
                            sha=parts[0],
                            message=parts[1],
                            author=parts[2],
                            short_sha=parts[3],
                        )
                    )
            return commits
        except subprocess.CalledProcessError:
            logger.warning("Failed to get commits ahead of main in %s", worktree)
            return []

    def fetch(self, worktree: Path, remote: str = "origin") -> bool:
        """Fetch from remote."""
        try:
            self._run_git(worktree, ["fetch", remote])
            return True
        except subprocess.CalledProcessError as e:
            logger.warning("Fetch failed: %s", e)
            return False

    def list_remote_branches(self, repo_root: Path, remote: str = "origin") -> list[str]:
        """List remote branches for a repository."""
        try:
            result = self._run_git(
                repo_root,
                ["branch", "-r", "--list", f"{remote}/*"],
            )
            return [line.strip() for line in result.stdout.splitlines() if line.strip()]
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to list remote branches in %s: %s", repo_root, e)
            return []

    def get_commits_ahead_count(
        self,
        repo_root: Path,
        branch: str,
        base: str = "origin/main",
    ) -> int:
        """Count commits ahead of base for a remote branch."""
        try:
            result = self._run_git(
                repo_root,
                ["rev-list", "--count", f"{base}..origin/{branch}"],
            )
            return int(result.stdout.strip() or 0)
        except (subprocess.CalledProcessError, ValueError) as e:
            logger.warning("Failed to count commits ahead for %s: %s", branch, e)
            return 0

    def get_last_commit_date(self, repo_root: Path, branch: str) -> str | None:
        """Get last commit date (relative) for a remote branch."""
        try:
            result = self._run_git(
                repo_root,
                ["log", "-1", "--format=%cr", f"origin/{branch}"],
            )
            return result.stdout.strip() or None
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to get last commit date for %s: %s", branch, e)
            return None

    def rebase_on_branch(
        self, worktree: Path, target: str = "origin/main"
    ) -> RebaseResult:
        """Rebase current branch onto target."""
        try:
            self._run_git(worktree, ["rebase", target])
            return RebaseResult(success=True, message=f"Rebased onto {target}")
        except subprocess.CalledProcessError as e:
            # Check for conflicts
            try:
                status = self._run_git(worktree, ["status", "--porcelain"])
                conflicts = [
                    line[3:] for line in status.stdout.split("\n")
                    if line.startswith("UU ")
                ]

                # Abort the rebase
                try:
                    self._run_git(worktree, ["rebase", "--abort"], check=False)
                    aborted = True
                except Exception:
                    aborted = False

                return RebaseResult(
                    success=False,
                    message=f"Rebase failed with conflicts",
                    conflicts=conflicts if conflicts else None,
                    aborted=aborted,
                )
            except Exception:
                return RebaseResult(
                    success=False,
                    message=f"Rebase failed: {e.stderr if hasattr(e, 'stderr') else str(e)}",
                )

    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        force_with_lease: bool = True,
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        """Push current branch to remote.

        Args:
            worktree: Path to the worktree.
            remote: Remote name (default: origin).
            force_with_lease: Use --force-with-lease (default: True).
            set_upstream: Set upstream tracking (default: True).
            skip_hooks: Skip pre-push hooks with --no-verify (default: False).
        """
        branch = self.get_current_branch(worktree)
        if not branch:
            return PushResult(
                success=False,
                branch="",
                remote=remote,
                message="Could not determine current branch",
                retryable=False,
            )

        args = ["push"]
        if skip_hooks:
            args.append("--no-verify")
        if set_upstream:
            args.extend(["-u", remote, branch])
        else:
            args.append(remote)

        if force_with_lease:
            args.append("--force-with-lease")

        start = time.monotonic()
        try:
            result = self._run_git(worktree, args)
            duration = time.monotonic() - start
            logger.info(
                "Push completed in %.2fs: branch=%s remote=%s skip_hooks=%s",
                duration,
                branch,
                remote,
                skip_hooks,
            )
            return PushResult(
                success=True,
                branch=branch,
                remote=remote,
                message=f"Pushed {branch} to {remote}",
            )
        except subprocess.CalledProcessError as e:
            duration = time.monotonic() - start
            error_msg = e.stderr if e.stderr else str(e)
            logger.warning(
                "Push failed in %.2fs: branch=%s remote=%s skip_hooks=%s error=%s",
                duration,
                branch,
                remote,
                skip_hooks,
                error_msg,
            )

            # Determine if retryable
            retryable = True
            if "non-fast-forward" in error_msg or "rejected" in error_msg:
                retryable = False  # Needs force or rebase
            if "permission denied" in error_msg.lower():
                retryable = False  # Auth issue

            return PushResult(
                success=False,
                branch=branch,
                remote=remote,
                message=error_msg,
                retryable=retryable,
            )

    def get_issue_number_from_branch(self, worktree: Path) -> int | None:
        """Extract issue number from branch name.

        First tries the canonical format ({issue_number}-{title}) via the
        centralized function. Falls back to legacy patterns for externally
        created branches.
        """
        from .._worktree_impl import extract_issue_number_from_branch

        branch = self.get_current_branch(worktree)
        if not branch:
            return None

        # Try canonical format first (e.g., "328-feature-name")
        issue_num = extract_issue_number_from_branch(branch)
        if issue_num is not None:
            return issue_num

        # Fallback patterns for legacy or externally created branches
        fallback_patterns = [
            r"issue-(\d+)",      # issue-123 (legacy format)
            r"/(\d+)-",          # feature/123-thing
        ]

        for pattern in fallback_patterns:
            match = re.search(pattern, branch)
            if match:
                return int(match.group(1))

        return None

    def get_worktree_root(self, worktree: Path) -> Path | None:
        """Get the root of the worktree (handles being in subdirectory)."""
        try:
            result = self._run_git(worktree, ["rev-parse", "--show-toplevel"])
            return Path(result.stdout.strip())
        except subprocess.CalledProcessError:
            return None

    def commit_all(
        self, worktree: Path, message: str, allow_empty: bool = False
    ) -> bool:
        """Stage all changes and commit.

        Args:
            worktree: Path to the worktree.
            message: Commit message.
            allow_empty: Whether to allow empty commits.

        Returns:
            True if commit succeeded, False otherwise.
        """
        try:
            # Stage all changes
            self._run_git(worktree, ["add", "-A"])

            # Commit
            args = ["commit", "-m", message]
            if allow_empty:
                args.append("--allow-empty")

            self._run_git(worktree, args)
            return True
        except subprocess.CalledProcessError as e:
            # "nothing to commit" is not an error
            if "nothing to commit" in (e.stdout or "") + (e.stderr or ""):
                return True
            logger.warning("Commit failed: %s", e)
            return False
