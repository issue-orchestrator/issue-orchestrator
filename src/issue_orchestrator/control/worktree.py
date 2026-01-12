"""Worktree preparation for sessions.

Cleans stale session artifacts before launching a new session in a reused
worktree.
"""

import logging
import shutil
from pathlib import Path

from ..infra.session_output import session_output_dir

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
    LEGACY_SESSION_LOG = "session.log"
    LEGACY_PANE_LOG = "pane.log"

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
        - per-session output directory (logs, completion, identity, notes)
        - legacy completion*.json/session-identity*.json files
        - legacy session.log/pane.log files

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
            removed_session_output = self._delete_session_output_dir(session_id)
            removed_completions = self._delete_files(self.COMPLETION_PATTERN)
            removed_identities = self._delete_files(self.SESSION_IDENTITY_PATTERN)
            removed_logs = self._delete_legacy_logs()
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

        if removed_session_output or removed_completions or removed_identities or removed_logs:
            logger.info(
                "[issue-%d] Removed session_output=%s, %d completion(s), %d identity file(s), legacy_logs=%s",
                self.issue_number,
                removed_session_output,
                len(removed_completions),
                len(removed_identities),
                removed_logs,
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

    def _delete_session_output_dir(self, session_id: str) -> bool:
        """Delete per-session output directory if it exists."""
        output_dir = session_output_dir(self.path, session_id)
        if not output_dir.exists():
            return False
        shutil.rmtree(output_dir, ignore_errors=False)
        return True

    def _delete_legacy_logs(self) -> bool:
        """Delete legacy log files if they exist."""
        removed = False
        for filename in (self.LEGACY_SESSION_LOG, self.LEGACY_PANE_LOG):
            log_path = self._orchestrator_dir / filename
            if not log_path.exists():
                continue
            try:
                log_path.unlink()
                removed = True
            except FileNotFoundError:
                continue
        return removed
