"""Worktree preparation for sessions.

Cleans stale session artifacts (completion.json, session-identity files)
before launching a new session in a reused worktree.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WorktreePreparationError(Exception):
    """Raised when worktree cannot be prepared for a new session.

    This indicates an unrecoverable problem (e.g., stale files that can't be
    deleted) that requires human intervention to resolve.
    """

    def __init__(self, path: Path, issue_number: int, message: str):
        self.path = path
        self.issue_number = issue_number
        super().__init__(message)


class Worktree:
    """Cleans stale session artifacts from a worktree.

    Usage:
        worktree = Worktree(path, issue_number)
        worktree.prepare_for_session(session_id)
    """

    ORCHESTRATOR_DIR = ".issue-orchestrator"
    COMPLETION_PATTERN = "completion*.json"
    SESSION_IDENTITY_PATTERN = "session-identity*.json"
    PANE_LOG = "pane.log"

    def __init__(self, path: Path, issue_number: int):
        """Initialize worktree.

        Args:
            path: Path to the worktree directory
            issue_number: Issue number (for logging)
        """
        self.path = path
        self.issue_number = issue_number
        self._orchestrator_dir = path / self.ORCHESTRATOR_DIR

    def prepare_for_session(self, session_id: str) -> None:
        """Prepare worktree for a new session by removing stale artifacts.

        Deletes:
        - completion*.json files (from previous sessions)
        - session-identity*.json files (from previous sessions)

        Args:
            session_id: The session ID that will own this worktree (for logging).

        Raises:
            WorktreePreparationError: If stale files cannot be deleted.
        """
        logger.info(
            "[issue-%d] Preparing worktree for session: path=%s session=%s",
            self.issue_number,
            self.path,
            session_id,
        )

        try:
            removed_completions = self._delete_files(self.COMPLETION_PATTERN)
            removed_identities = self._delete_files(self.SESSION_IDENTITY_PATTERN)
            removed_pane_log = self._delete_pane_log()
        except OSError as e:
            logger.error(
                "[issue-%d] Failed to clean stale files in worktree: %s",
                self.issue_number,
                e,
            )
            raise WorktreePreparationError(
                self.path,
                self.issue_number,
                f"Cannot delete stale files in worktree {self.path.name}: {e}",
            ) from e

        if removed_completions or removed_identities or removed_pane_log:
            logger.info(
                "[issue-%d] Removed %d completion(s), %d identity file(s), pane.log=%s",
                self.issue_number,
                len(removed_completions),
                len(removed_identities),
                removed_pane_log,
            )

    def _delete_files(self, pattern: str) -> list[Path]:
        """Delete files matching pattern in orchestrator directory.

        Args:
            pattern: Glob pattern to match files.

        Returns:
            List of deleted file paths.

        Raises:
            OSError: If any file cannot be deleted.
        """
        removed: list[Path] = []

        if not self._orchestrator_dir.exists():
            return removed

        for file_path in self._orchestrator_dir.glob(pattern):
            try:
                file_path.unlink()
            except FileNotFoundError:
                # Another process may have removed it between glob and unlink.
                continue
            removed.append(file_path)

        return removed

    def _delete_pane_log(self) -> bool:
        """Delete pane.log if it exists.

        Returns:
            True if file was deleted, False otherwise.

        Raises:
            OSError: If file cannot be deleted.
        """
        pane_log = self._orchestrator_dir / self.PANE_LOG
        if not pane_log.exists():
            return False

        try:
            pane_log.unlink()
            return True
        except FileNotFoundError:
            # Race condition - file was deleted between exists() and unlink()
            return False
