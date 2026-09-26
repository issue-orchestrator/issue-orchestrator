"""Shared source-content policy for live completion and retained publication."""

from collections.abc import Callable
from pathlib import Path

from ..domain.pr_issue_reference import names_issue_in_closing_keyword
from ..infra.runtime_artifacts import (
    build_forbidden_runtime_artifact_reason, forbidden_branch_runtime_artifacts,
)
from ..ports.publication_source import PublicationSourceReader
from .test_skip_guard import added_test_paths, scan_added_test_skip_guards


class PublicationSourceGuards:
    def __init__(self, working_copy: PublicationSourceReader, base_branch: Callable[[], str]) -> None:
        self._working_copy = working_copy
        self._base_branch = base_branch

    def test_skips(self, worktree: Path) -> str | None:
        base_ref = f"origin/{self._base_branch()}"
        diff = self._working_copy.diff_against_base(worktree, base_ref)
        if not diff.success:
            return f"Could not scan branch diff for banned test skips against {base_ref}: {diff.error or 'unknown git error'}"
        try:
            paths = added_test_paths(diff.diff_text)
        except ValueError as exc:
            return f"Could not parse branch diff for banned test skips: {exc}"
        if not paths:
            return None
        files = self._working_copy.read_branch_text_files(worktree, paths)
        if not files.success:
            return f"Could not read branch-tip test files for banned test-skip scan: {files.error or 'unknown git error'}"
        try:
            scan = scan_added_test_skip_guards(diff.diff_text, files.files)
        except ValueError as exc:
            return f"Could not scan branch-tip test files for banned test skips: {exc}"
        return None if scan.ok else scan.reason()

    def runtime_artifacts(self, worktree: Path) -> str | None:
        base_ref = f"origin/{self._base_branch()}"
        paths = self._working_copy.branch_post_image_paths_against_base(worktree, base_ref)
        if not paths.success:
            return f"Could not scan branch paths for runtime artifacts against {base_ref}: {paths.error or 'unknown git error'}"
        forbidden = forbidden_branch_runtime_artifacts(paths.paths)
        return build_forbidden_runtime_artifact_reason(forbidden) if forbidden else None

    def partial_delivery(self, worktree: Path, issue_number: int) -> str | None:
        """Why the branch's commits would close an issue declared partly delivered.

        GitHub closes an issue named by a closing keyword in any commit
        message that reaches the default branch, and a squash merge usually
        copies those messages into its own. So a partial delivery must not
        carry one in any commit (#7288).
        """
        base_ref = f"origin/{self._base_branch()}"
        commits = self._working_copy.branch_commit_messages_against_base(worktree, base_ref)
        if not commits.success:
            return (
                f"Could not read branch commit messages against {base_ref} for the "
                f"partial-delivery check: {commits.error or 'unknown git error'}"
            )
        closing = [
            message.splitlines()[0]
            for message in commits.messages
            if names_issue_in_closing_keyword(message, issue_number)
        ]
        if not closing:
            return None
        return (
            f"completion declared partial delivery of #{issue_number}, but "
            f"{len(closing)} commit(s) close it by keyword (first: {closing[0]!r}); "
            f"GitHub would close #{issue_number} on merge. Reword them to "
            f"'Refs #{issue_number}' or publish without --partial"
        )

    def check(self, worktree: Path) -> str | None:
        return self.test_skips(worktree) or self.runtime_artifacts(worktree)
