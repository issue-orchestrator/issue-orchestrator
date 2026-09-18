"""Unit tests for the worktree policy module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from issue_orchestrator.adapters.worktree.removal import CheckoutRemoval
from issue_orchestrator.adapters.worktree.worktree_policy import (
    ValidateOrDeletePolicy,
)
from issue_orchestrator.ports.worktree_policy import (
    ValidationResult,
    SyncResult,
)


class TestValidateOrDeletePolicy:
    """Test the ValidateOrDeletePolicy class."""

    def test_validate_nonexistent_worktree(self, tmp_path):
        """Test validation fails for nonexistent worktree."""
        policy = ValidateOrDeletePolicy()
        nonexistent = tmp_path / "does-not-exist"

        result = policy.validate_for_reuse(nonexistent, None, tmp_path)

        assert result.can_reuse is False
        assert "does not exist" in result.reason

    def test_validate_not_git_worktree(self, tmp_path):
        """Test validation fails for directory without .git."""
        policy = ValidateOrDeletePolicy()
        not_git = tmp_path / "not-git"
        not_git.mkdir()

        result = policy.validate_for_reuse(not_git, None, tmp_path)

        assert result.can_reuse is False
        assert "no .git" in result.reason

    def test_validate_valid_worktree(self, tmp_path):
        """Test validation passes for valid worktree."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/path")

        with patch.object(policy, "_check_broken_git_state", return_value=None):
            result = policy.validate_for_reuse(worktree, None, tmp_path)

        assert result.can_reuse is True

    def test_validate_rebase_in_progress(self, tmp_path):
        """Test validation fails when rebase is in progress."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_dir = worktree / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-merge").mkdir()

        result = policy.validate_for_reuse(worktree, None, tmp_path)

        assert result.can_reuse is False
        assert "rebase in progress" in result.reason

    def test_validate_branch_mismatch(self, tmp_path):
        """Test validation fails when branch doesn't match expected."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/path")

        with patch.object(policy, "_check_broken_git_state", return_value=None):
            with patch.object(policy, "_get_current_branch", return_value="wrong-branch"):
                result = policy.validate_for_reuse(worktree, "expected-branch", tmp_path)

        assert result.can_reuse is False
        assert "branch mismatch" in result.reason

    def test_validate_branch_matches(self, tmp_path):
        """Test validation passes when branch matches expected."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text("gitdir: /some/path")

        with patch.object(policy, "_check_broken_git_state", return_value=None):
            with patch.object(policy, "_get_current_branch", return_value="expected-branch"):
                result = policy.validate_for_reuse(worktree, "expected-branch", tmp_path)

        assert result.can_reuse is True


class TestSyncRemoteRefs:
    """Test remote ref syncing."""

    def test_sync_success(self, tmp_path):
        """Test sync succeeds when fetch succeeds."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
        ) as mock_git:
            mock_git.return_value = MagicMock(returncode=0, stderr="")
            result = policy.sync_remote_refs(worktree, "my-branch")

        assert result.success is True

    def test_sync_first_push(self, tmp_path):
        """Test sync succeeds for first push (no remote branch)."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
        ) as mock_git:
            mock_git.side_effect = [
                MagicMock(returncode=1, stderr="couldn't find remote ref"),
                MagicMock(returncode=1, stderr=""),
            ]
            result = policy.sync_remote_refs(worktree, "new-branch")

        assert result.success is True
        assert "first push" in result.reason

    def test_sync_network_failure(self, tmp_path):
        """Test sync fails on network error."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
        ) as mock_git:
            mock_git.return_value = MagicMock(
                returncode=1, stderr="Could not resolve host"
            )
            result = policy.sync_remote_refs(worktree, "my-branch")

        assert result.success is False
        assert "fetch failed" in result.reason

    def test_sync_first_push_fails_with_stale_remote_ref(self, tmp_path):
        """Test first push fails when a stale remote ref exists locally."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
        ) as mock_git:
            mock_git.side_effect = [
                MagicMock(returncode=1, stderr="couldn't find remote ref"),
                MagicMock(returncode=0, stderr=""),
            ]
            result = policy.sync_remote_refs(worktree, "stale-branch")

        assert result.success is False
        assert "stale remote tracking ref" in result.reason


class TestDeleteWorktree:
    """Test worktree deletion."""

    def test_delete_asks_the_removal_owner(self, tmp_path):
        """It has no removal of its own: git-then-directory lives in one place.

        A second copy here was a second custody window, which is what #7274
        rounds 2 and 3 kept finding.
        """
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy.remove_checkout_path"
        ) as mock_remove:
            mock_remove.return_value = CheckoutRemoval(
                removed=True, used_filesystem_fallback=False
            )
            result = policy.delete_worktree(worktree, tmp_path)

        assert result is True
        assert mock_remove.call_args.args == (worktree,)
        assert mock_remove.call_args.kwargs["force"] is True
        assert mock_remove.call_args.kwargs["repo_root"] == tmp_path

    def test_delete_falls_back_to_the_directory_when_git_declines(self, tmp_path):
        """Still true end to end -- the owner does it, under one custody answer.

        The repository is real: custody is read out of its metadata, and a
        ``repo_root`` that is not a repository is now refused rather than
        quietly answering "nothing held" (#7274 round 9 finding 1).
        """
        policy = ValidateOrDeletePolicy()
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "file.txt").write_text("content")

        result = policy.delete_worktree(worktree, repo_root)

        assert result is True
        assert not worktree.exists()


