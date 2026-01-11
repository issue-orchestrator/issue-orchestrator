"""Worktree setup policy implementation - validate or delete.

This module implements the WorktreePolicy protocol with a
"validate or delete" strategy:
- If worktree is in good state, reuse it
- If anything is wrong, delete and let fresh creation handle it
"""

import logging
import shutil
from pathlib import Path

from ...ports.worktree_policy import (
    ValidationResult,
    SyncResult,
    WorktreePolicy,
)
from ._worktree import _git_run, remove_worktree

logger = logging.getLogger(__name__)


class ValidateOrDeletePolicy:
    """Worktree policy: validate state or delete for fresh start.

    This implementation follows the principle that reuse is an
    optimization, not a requirement. If anything prevents a clean
    setup, we delete the worktree and let fresh creation handle it.

    Checks performed:
    1. Worktree exists and is valid git worktree
    2. Not in broken git state (mid-rebase, mid-merge, conflicts)
    3. Branch matches expected (if specified)
    4. Remote refs can be synced
    """

    def validate_for_reuse(
        self,
        worktree_path: Path,
        expected_branch: str | None,
        repo_root: Path,
    ) -> ValidationResult:
        """Check if a worktree can be reused."""
        worktree_path = Path(worktree_path)

        # Check worktree exists
        if not worktree_path.exists():
            return ValidationResult(
                can_reuse=False,
                reason="worktree path does not exist",
            )

        # Check it's a valid git worktree
        git_marker = worktree_path / ".git"
        if not git_marker.exists():
            return ValidationResult(
                can_reuse=False,
                reason="not a valid git worktree (no .git)",
            )

        # Check not in broken git state
        broken_state = self._check_broken_git_state(worktree_path)
        if broken_state:
            return ValidationResult(
                can_reuse=False,
                reason=f"broken git state: {broken_state}",
            )

        # Check branch matches (if expected_branch provided)
        if expected_branch:
            current_branch = self._get_current_branch(worktree_path)
            if current_branch is None:
                return ValidationResult(
                    can_reuse=False,
                    reason="could not determine current branch",
                )
            if current_branch != expected_branch:
                return ValidationResult(
                    can_reuse=False,
                    reason=f"branch mismatch: expected {expected_branch}, found {current_branch}",
                )

        return ValidationResult(can_reuse=True, reason="validation passed")

    def sync_remote_refs(
        self,
        worktree_path: Path,
        branch_name: str,
    ) -> SyncResult:
        """Sync local tracking refs with remote."""
        worktree_path = Path(worktree_path)

        # Try to fetch the specific branch
        result = _git_run(
            worktree_path,
            ["fetch", "origin", branch_name],
            check=False,
        )

        if result.returncode == 0:
            logger.debug(
                "[POLICY] Remote refs synced for branch %s in %s",
                branch_name,
                worktree_path,
            )
            return SyncResult(success=True, reason="refs synced")

        stderr = result.stderr or ""

        # "couldn't find remote ref" means branch doesn't exist on remote - OK
        if "couldn't find remote ref" in stderr:
            logger.debug(
                "[POLICY] Branch %s not on remote yet (first push) - OK",
                branch_name,
            )
            return SyncResult(success=True, reason="first push (no remote branch)")

        # Any other error means we can't sync refs
        reason = f"fetch failed: {stderr.strip()}"
        logger.warning("[POLICY] Failed to sync remote refs for %s: %s", branch_name, reason)
        return SyncResult(success=False, reason=reason)

    def delete_worktree(
        self,
        worktree_path: Path,
        repo_root: Path,
    ) -> bool:
        """Delete a worktree completely."""
        worktree_path = Path(worktree_path)
        logger.info("[POLICY] Deleting worktree for fresh start: %s", worktree_path)

        try:
            # Try git worktree remove first (clean removal)
            remove_worktree(worktree_path)
            return True
        except Exception as e:
            logger.warning("[POLICY] git worktree remove failed: %s, trying rmtree", e)

        # Fallback: just delete the directory
        try:
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)
            # Also prune from git's worktree list
            _git_run(repo_root, ["worktree", "prune"], check=False)
            return True
        except Exception as e:
            logger.error("[POLICY] Failed to delete worktree %s: %s", worktree_path, e)
            return False

    def _check_broken_git_state(self, worktree_path: Path) -> str | None:
        """Check if worktree is in a broken git state.

        Returns:
            Description of broken state, or None if clean
        """
        git_dir = self._resolve_git_dir(worktree_path)

        # Check for rebase in progress
        rebase_markers = [
            git_dir / "rebase-merge",
            git_dir / "rebase-apply",
        ]
        for marker in rebase_markers:
            if marker.exists():
                return "rebase in progress"

        # Check for merge in progress
        result = _git_run(
            worktree_path,
            ["rev-parse", "--verify", "MERGE_HEAD"],
            check=False,
        )
        if result.returncode == 0:
            return "merge in progress"

        # Check for cherry-pick in progress
        if (git_dir / "CHERRY_PICK_HEAD").exists():
            return "cherry-pick in progress"

        # Check for unresolved conflicts
        result = _git_run(
            worktree_path,
            ["diff", "--check"],
            check=False,
        )
        if result.returncode != 0 and "conflict" in (result.stdout or "").lower():
            return "unresolved conflicts"

        return None

    def _resolve_git_dir(self, worktree_path: Path) -> Path:
        """Resolve the actual .git directory for a worktree.

        Worktrees have a .git file that points to the real git dir.
        """
        git_marker = worktree_path / ".git"

        if git_marker.is_dir():
            return git_marker

        if git_marker.is_file():
            try:
                content = git_marker.read_text().strip()
                if content.startswith("gitdir: "):
                    return Path(content[8:])
            except Exception:
                pass

        return git_marker

    def _get_current_branch(self, worktree_path: Path) -> str | None:
        """Get the current branch of a worktree.

        Returns:
            Branch name, or None if detached HEAD or error
        """
        result = _git_run(
            worktree_path,
            ["rev-parse", "--abbrev-ref", "HEAD"],
            check=False,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        if branch == "HEAD":
            return None  # Detached HEAD
        return branch


# Default policy instance
default_policy: WorktreePolicy = ValidateOrDeletePolicy()
