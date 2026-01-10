"""Worktree domain model.

Encapsulates worktree state and lifecycle operations. A worktree is a
first-class concept that tracks its readiness for sessions.

Key insight: worktrees persist across sessions and may have stale artifacts
from previous sessions (completion.json, session-identity files). This model
provides state awareness and preparation operations.
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class WorktreeState(Enum):
    """Observable state of a worktree."""

    CLEAN = "clean"  # Ready for new session
    HAS_UNCOMMITTED = "uncommitted"  # Dirty working tree
    HAS_STALE_COMPLETION = "stale"  # completion.json from previous session
    REBASE_CONFLICT = "conflict"  # Unresolved merge conflict
    MISSING = "missing"  # Path doesn't exist


@dataclass
class WorktreeStatus:
    """Current status of a worktree."""

    state: WorktreeState
    path: Path
    branch: str

    # Optional details
    uncommitted_files: list[str] = field(default_factory=list)
    stale_completion_paths: list[Path] = field(default_factory=list)
    stale_completion_session_ids: list[str] = field(default_factory=list)
    commits_ahead_of_main: int = 0

class Worktree:
    """Model for a git worktree's session-related state.

    Encapsulates worktree state inspection and preparation operations.
    Key operation is prepare_for_session() which cleans stale artifacts.

    Usage:
        worktree = Worktree(path, branch, issue_number)
        worktree.prepare_for_session(session_id)  # Cleans stale artifacts
    """

    # Directory and file patterns for session artifacts
    ORCHESTRATOR_DIR = ".issue-orchestrator"
    COMPLETION_PATTERN = "completion*.json"
    SESSION_IDENTITY_PATTERN = "session-identity*.json"

    def __init__(self, path: Path, branch: str, issue_number: int):
        """Initialize a worktree model.

        Args:
            path: Path to the worktree directory
            branch: Git branch name
            issue_number: Issue number (for logging)
        """
        self.path = path
        self.branch = branch
        self.issue_number = issue_number
        self._orchestrator_dir = path / self.ORCHESTRATOR_DIR

    def get_status(self, expected_session_id: str | None = None) -> WorktreeStatus:
        """Get current worktree state by inspecting filesystem.

        Args:
            expected_session_id: If provided, completion.json with this
                session_id is considered current, not stale.

        Returns:
            WorktreeStatus with current state and details.
        """
        if not self.path.exists():
            return WorktreeStatus(
                state=WorktreeState.MISSING,
                path=self.path,
                branch=self.branch,
            )

        # Check for stale completion files
        stale_paths, stale_session_ids = self._find_stale_completions(expected_session_id)

        if stale_paths:
            return WorktreeStatus(
                state=WorktreeState.HAS_STALE_COMPLETION,
                path=self.path,
                branch=self.branch,
                stale_completion_paths=stale_paths,
                stale_completion_session_ids=stale_session_ids,
            )

        # Check for uncommitted changes
        uncommitted = self._get_uncommitted_files()
        if uncommitted:
            return WorktreeStatus(
                state=WorktreeState.HAS_UNCOMMITTED,
                path=self.path,
                branch=self.branch,
                uncommitted_files=uncommitted,
            )

        # Check for rebase conflict
        if self._has_rebase_conflict():
            return WorktreeStatus(
                state=WorktreeState.REBASE_CONFLICT,
                path=self.path,
                branch=self.branch,
            )

        return WorktreeStatus(
            state=WorktreeState.CLEAN,
            path=self.path,
            branch=self.branch,
        )

    def prepare_for_session(self, session_id: str) -> WorktreeStatus:
        """Prepare worktree for a new session.

        1. Remove stale completion*.json files
        2. Remove stale session-identity*.json files

        Args:
            session_id: The session ID that will own this worktree.

        Returns:
            New status after preparation.
        """
        logger.info(
            "[issue-%d] Preparing worktree for session: path=%s session=%s",
            self.issue_number,
            self.path,
            session_id,
        )

        # Clean stale completion files
        removed_completions = self._cleanup_completion_files(session_id)
        if removed_completions:
            logger.info(
                "[issue-%d] Removed %d stale completion file(s): %s",
                self.issue_number,
                len(removed_completions),
                [str(p.name) for p in removed_completions],
            )

        # Clean stale session identity files
        removed_identities = self._cleanup_session_identity_files()
        if removed_identities:
            logger.info(
                "[issue-%d] Removed %d stale session identity file(s): %s",
                self.issue_number,
                len(removed_identities),
                [str(p.name) for p in removed_identities],
            )

        # Return new status
        return self.get_status(session_id)

    def cleanup_session_artifacts(self) -> None:
        """Remove all session artifacts without removing worktree.

        Called after session completion processing to prevent
        stale artifacts on next session.
        """
        logger.info(
            "[issue-%d] Cleaning session artifacts from worktree: path=%s",
            self.issue_number,
            self.path,
        )

        removed_completions = self._cleanup_completion_files(keep_session_id=None)
        removed_identities = self._cleanup_session_identity_files()

        if removed_completions or removed_identities:
            logger.info(
                "[issue-%d] Removed %d completion(s) and %d identity file(s)",
                self.issue_number,
                len(removed_completions),
                len(removed_identities),
            )

    def _find_stale_completions(
        self, expected_session_id: str | None
    ) -> tuple[list[Path], list[str]]:
        """Find completion files that don't match expected session.

        Returns:
            Tuple of (paths, session_ids) for stale completions.
        """
        stale_paths: list[Path] = []
        stale_session_ids: list[str] = []

        if not self._orchestrator_dir.exists():
            return stale_paths, stale_session_ids

        for completion_file in self._orchestrator_dir.glob(self.COMPLETION_PATTERN):
            try:
                content = completion_file.read_text()
                record = json.loads(content)
                file_session_id = record.get("session_id", "")

                # If no expected session_id, all completions are stale
                # If expected doesn't match, it's stale
                if expected_session_id is None or file_session_id != expected_session_id:
                    stale_paths.append(completion_file)
                    stale_session_ids.append(file_session_id)
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(
                    "[issue-%d] Could not read completion file %s: %s",
                    self.issue_number,
                    completion_file,
                    e,
                )
                # Treat unreadable files as stale
                stale_paths.append(completion_file)
                stale_session_ids.append("<unreadable>")

        return stale_paths, stale_session_ids

    def _cleanup_completion_files(self, keep_session_id: str | None = None) -> list[Path]:
        """Remove completion files, optionally keeping one that matches session_id.

        Args:
            keep_session_id: If provided, don't delete files with this session_id.

        Returns:
            List of removed file paths.
        """
        removed: list[Path] = []

        if not self._orchestrator_dir.exists():
            return removed

        for completion_file in self._orchestrator_dir.glob(self.COMPLETION_PATTERN):
            should_remove = True

            if keep_session_id:
                try:
                    content = completion_file.read_text()
                    record = json.loads(content)
                    if record.get("session_id") == keep_session_id:
                        should_remove = False
                except (json.JSONDecodeError, OSError):
                    pass  # Remove unreadable files

            if should_remove:
                try:
                    completion_file.unlink()
                    removed.append(completion_file)
                except OSError as e:
                    logger.warning(
                        "[issue-%d] Failed to remove stale completion %s: %s",
                        self.issue_number,
                        completion_file,
                        e,
                    )

        return removed

    def _cleanup_session_identity_files(self) -> list[Path]:
        """Remove all session identity files.

        Returns:
            List of removed file paths.
        """
        removed: list[Path] = []

        if not self._orchestrator_dir.exists():
            return removed

        for identity_file in self._orchestrator_dir.glob(self.SESSION_IDENTITY_PATTERN):
            try:
                identity_file.unlink()
                removed.append(identity_file)
            except OSError as e:
                logger.warning(
                    "[issue-%d] Failed to remove session identity %s: %s",
                    self.issue_number,
                    identity_file,
                    e,
                )

        return removed

    def _get_uncommitted_files(self) -> list[str]:
        """Get list of uncommitted files in worktree.

        Note: Currently returns empty list. Full detection would require
        subprocess (git status) which is not allowed in control layer.
        The HAS_UNCOMMITTED state exists for future use when this is
        refactored to use a port. The key functionality (stale completion
        detection) works without this.

        Returns:
            Empty list (uncommitted detection not implemented).
        """
        # Would need: git status --porcelain
        # Control layer cannot import subprocess - use port if needed
        return []

    def _has_rebase_conflict(self) -> bool:
        """Check if worktree has unresolved rebase conflict.

        Returns:
            True if in a rebase with conflicts.
        """
        rebase_dir = self.path / ".git" / "rebase-merge"
        rebase_apply = self.path / ".git" / "rebase-apply"

        # For worktrees, .git is a file pointing to the main repo
        git_path = self.path / ".git"
        if git_path.is_file():
            try:
                content = git_path.read_text().strip()
                if content.startswith("gitdir:"):
                    gitdir = Path(content[7:].strip())
                    rebase_dir = gitdir / "rebase-merge"
                    rebase_apply = gitdir / "rebase-apply"
            except OSError:
                pass

        return rebase_dir.exists() or rebase_apply.exists()
