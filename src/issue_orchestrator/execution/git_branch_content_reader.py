"""Read committed branch content for publication policy.

Two of the :class:`~issue_orchestrator.ports.publication_source.PublicationSourceReader`
reads: branch-tip text files, and the full messages of the commits a branch
adds. Both are read byte-exactly through the working copy's git transport, so
a message's own newlines and a file's line endings reach policy unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ..ports.git import GitError, GitResult
from ..ports.working_copy import (
    BranchCommitMessagesResult,
    BranchTextFile,
    BranchTextFilesResult,
)

logger = logging.getLogger(__name__)


class GitBranchContentReader:
    """Branch content reads over the working copy's exact git transport."""

    def __init__(
        self,
        run_exact: Callable[[Path, list[str]], GitResult],
        run_nul_records: Callable[[Path, list[str]], list[str]],
    ) -> None:
        self._run_exact = run_exact
        self._run_nul_records = run_nul_records

    def text_files(self, worktree: Path, paths: tuple[str, ...]) -> BranchTextFilesResult:
        """Exact tracked ``HEAD`` content for selected text files."""
        files: list[BranchTextFile] = []
        try:
            for path in paths:
                result = self._run_exact(worktree, ["show", f"HEAD:{path}"])
                files.append(BranchTextFile(path=path, content=result.stdout))
            return BranchTextFilesResult(success=True, files=tuple(files))
        except GitError as exc:
            error = git_error_output(exc)
            logger.warning("Failed to read branch-tip text files in %s: %s", worktree, error)
            return BranchTextFilesResult(success=False, error=error)

    def commit_messages_against_base(
        self, worktree: Path, base_ref: str
    ) -> BranchCommitMessagesResult:
        """Full message of every commit in ``HEAD`` that ``base_ref`` lacks.

        ``-z`` ends each record with NUL, so multi-line messages stay whole.
        """
        try:
            messages = self._run_nul_records(
                worktree, ["log", "-z", "--format=%B", f"{base_ref}..HEAD"]
            )
            return BranchCommitMessagesResult(success=True, messages=tuple(messages))
        except GitError as exc:
            error = git_error_output(exc)
            logger.warning(
                "Failed to read commit messages against %s in %s: %s", base_ref, worktree, error
            )
            return BranchCommitMessagesResult(success=False, error=error)


def git_error_output(error: GitError) -> str:
    """Return the full user-facing output from a failed git command."""
    parts: list[str] = []
    stdout = (error.result.stdout or "").strip()
    stderr = (error.result.stderr or "").strip()
    if stdout:
        parts.append(stdout)
    if stderr and stderr != stdout:
        parts.append(stderr)
    if parts:
        return "\n".join(parts)
    return str(error)
