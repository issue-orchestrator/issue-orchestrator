"""Reviewer worktree manager: create, fast-forward, remove."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.execution.reviewer_worktree import (
    ReviewerWorktreeError,
    create_reviewer_worktree,
    fast_forward_reviewer_worktree,
    remove_reviewer_worktree,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _bootstrap_repo_with_branch(tmp_path: Path) -> tuple[Path, Path, str]:
    """Build a tiny git repo with a feature branch checked out in a coder worktree."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-q", "-b", "main")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")
    (repo_root / "README").write_text("hello\n")
    _git(repo_root, "add", "README")
    _git(repo_root, "commit", "-q", "-m", "initial")

    coder_worktree = tmp_path / "coder-wt"
    branch = "feature/widget"
    _git(repo_root, "worktree", "add", "-b", branch, str(coder_worktree))
    (coder_worktree / "work.py").write_text("print('first')\n")
    _git(coder_worktree, "add", "work.py")
    _git(coder_worktree, "commit", "-q", "-m", "first commit")
    return repo_root, coder_worktree, branch


class TestReviewerWorktreeLifecycle:
    def test_create_attaches_sibling_at_coder_branch_tip(self, tmp_path: Path) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)

        reviewer = create_reviewer_worktree(
            coder_worktree=coder,
            coder_branch=branch,
            timestamp="20260502T000000Z",
        )

        assert reviewer.path == coder.parent / f"{coder.name}-review-20260502T000000Z"
        assert reviewer.path.exists()
        assert reviewer.path.is_dir()
        # Detached HEAD: HEAD points at the same SHA as the coder branch tip.
        coder_tip = _git(repo_root, "rev-parse", branch)
        reviewer_head = _git(reviewer.path, "rev-parse", "HEAD")
        assert reviewer_head == coder_tip
        # And HEAD is detached, not on the coder's branch.
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=reviewer.path, capture_output=True, text=True,
        )
        assert symbolic.returncode != 0, "reviewer worktree must be detached"

    def test_create_refuses_to_clobber_existing_path(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        sibling = coder.parent / f"{coder.name}-review-T"
        sibling.mkdir()

        with pytest.raises(ReviewerWorktreeError, match="already exists"):
            create_reviewer_worktree(
                coder_worktree=coder, coder_branch=branch, timestamp="T",
            )

    def test_fast_forward_picks_up_new_coder_commits(self, tmp_path: Path) -> None:
        repo_root, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder, coder_branch=branch, timestamp="T",
        )
        original_tip = _git(repo_root, "rev-parse", branch)

        # Coder commits more work after the reviewer was created.
        (coder / "work.py").write_text("print('second')\n")
        _git(coder, "add", "work.py")
        _git(coder, "commit", "-q", "-m", "second commit")
        new_tip = _git(repo_root, "rev-parse", branch)
        assert new_tip != original_tip

        fast_forward_reviewer_worktree(reviewer)

        reviewer_head = _git(reviewer.path, "rev-parse", "HEAD")
        assert reviewer_head == new_tip

    def test_remove_deletes_the_worktree(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder, coder_branch=branch, timestamp="T",
        )
        assert reviewer.path.exists()

        remove_reviewer_worktree(reviewer)

        assert not reviewer.path.exists()

    def test_remove_is_noop_when_path_already_gone(self, tmp_path: Path) -> None:
        _, coder, branch = _bootstrap_repo_with_branch(tmp_path)
        reviewer = create_reviewer_worktree(
            coder_worktree=coder, coder_branch=branch, timestamp="T",
        )
        # External cleanup beats us to it.
        import shutil
        shutil.rmtree(reviewer.path)

        # Must not raise — orchestrator shutdown paths rely on idempotence.
        remove_reviewer_worktree(reviewer)
