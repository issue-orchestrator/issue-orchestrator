"""Read committed branch content for publication policy without mutation authority."""

from pathlib import Path
from typing import Protocol

from .working_copy import BranchPathsResult, BranchTextFilesResult, DiffResult


class PublicationSourceReader(Protocol):
    def diff_against_base(self, worktree: Path, base_ref: str) -> DiffResult: ...
    def read_branch_text_files(self, worktree: Path, paths: tuple[str, ...]) -> BranchTextFilesResult: ...
    def branch_post_image_paths_against_base(self, worktree: Path, base_ref: str) -> BranchPathsResult: ...
