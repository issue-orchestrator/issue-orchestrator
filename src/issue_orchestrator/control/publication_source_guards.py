"""Shared source-content policy for live completion and retained publication."""

from collections.abc import Callable
from pathlib import Path

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

    def branch_paths(self, worktree: Path, base_branch: str | None) -> tuple[str, ...]:
        """Every path the branch's diff touches against *base_branch* (the default
        base when None), deleted and renamed-from paths included; raises when
        git cannot say."""
        base_ref = f"origin/{base_branch or self._base_branch()}"
        paths = self._working_copy.branch_touched_paths_against_base(worktree, base_ref)
        if not paths.success:
            raise RuntimeError(
                f"Could not read the branch's changed paths against {base_ref}: {paths.error or 'unknown git error'}"
            )
        return paths.paths

    def branch_commit_messages(self, worktree: Path) -> tuple[tuple[str, ...], str | None]:
        """The full messages of the branch's own commits, or why they are unreadable."""
        base_ref = f"origin/{self._base_branch()}"
        commits = self._working_copy.branch_commit_messages_against_base(worktree, base_ref)
        if not commits.success:
            return (), (
                f"Could not read branch commit messages against {base_ref}: "
                f"{commits.error or 'unknown git error'}"
            )
        return commits.messages, None

    def check(self, worktree: Path) -> str | None:
        return self.test_skips(worktree) or self.runtime_artifacts(worktree)
