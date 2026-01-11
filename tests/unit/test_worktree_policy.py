"""Unit tests for the worktree policy module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            mock_git.return_value = MagicMock(
                returncode=1, stderr="couldn't find remote ref"
            )
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


class TestDeleteWorktree:
    """Test worktree deletion."""

    def test_delete_calls_remove_worktree(self, tmp_path):
        """Test delete uses remove_worktree function."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy.remove_worktree"
        ) as mock_remove:
            result = policy.delete_worktree(worktree, tmp_path)

        assert result is True
        mock_remove.assert_called_once_with(worktree)

    def test_delete_fallback_to_rmtree(self, tmp_path):
        """Test delete falls back to rmtree if git remove fails."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / "file.txt").write_text("content")

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy.remove_worktree",
            side_effect=Exception("git remove failed"),
        ):
            with patch(
                "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
            ) as mock_git:
                mock_git.return_value = MagicMock(returncode=0)
                result = policy.delete_worktree(worktree, tmp_path)

        assert result is True
        assert not worktree.exists()


class TestBrokenGitStateDetection:
    """Test detection of broken git states."""

    def test_detect_rebase_merge(self, tmp_path):
        """Test detection of rebase-merge in progress."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_dir = worktree / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-merge").mkdir()

        result = policy._check_broken_git_state(worktree)

        assert result == "rebase in progress"

    def test_detect_rebase_apply(self, tmp_path):
        """Test detection of rebase-apply in progress."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_dir = worktree / ".git"
        git_dir.mkdir()
        (git_dir / "rebase-apply").mkdir()

        result = policy._check_broken_git_state(worktree)

        assert result == "rebase in progress"

    def test_detect_cherry_pick(self, tmp_path):
        """Test detection of cherry-pick in progress."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_dir = worktree / ".git"
        git_dir.mkdir()
        (git_dir / "CHERRY_PICK_HEAD").write_text("abc123")

        result = policy._check_broken_git_state(worktree)

        assert result == "cherry-pick in progress"

    def test_clean_state(self, tmp_path):
        """Test clean state returns None."""
        policy = ValidateOrDeletePolicy()
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        git_dir = worktree / ".git"
        git_dir.mkdir()

        with patch(
            "issue_orchestrator.adapters.worktree.worktree_policy._git_run"
        ) as mock_git:
            # Mock both git commands to succeed
            mock_git.return_value = MagicMock(returncode=1, stdout="")
            result = policy._check_broken_git_state(worktree)

        assert result is None
