"""Unit tests for E2E worktree management."""

import subprocess

import pytest
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.infra.e2e_worktree import (
    ensure_e2e_worktree,
    get_e2e_worktree_path,
)


class TestGetE2EWorktreePath:
    """Test deterministic worktree path derivation."""

    def test_worktree_path_is_sibling_directory(self, tmp_path: Path):
        repo_root = tmp_path / "issue-orchestrator"
        repo_root.mkdir()

        result = get_e2e_worktree_path(repo_root)

        assert result == tmp_path / "issue-orchestrator-e2e-worktree"
        assert result.parent == repo_root.parent

    def test_worktree_path_appends_suffix(self, tmp_path: Path):
        repo_root = tmp_path / "my-repo"
        repo_root.mkdir()

        result = get_e2e_worktree_path(repo_root)

        assert result.name == "my-repo-e2e-worktree"


class TestEnsureE2EWorktree:
    """Test worktree creation, update, and recovery."""

    @pytest.fixture
    def repo_root(self, tmp_path: Path) -> Path:
        root = tmp_path / "issue-orchestrator"
        root.mkdir()
        return root

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_creates_worktree_when_not_exists(self, mock_run, repo_root: Path):
        """When the worktree directory doesn't exist, create it."""
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        expected_wt = get_e2e_worktree_path(repo_root)

        result = ensure_e2e_worktree(repo_root)

        assert result == expected_wt

        # First call: git worktree add
        git_add_call = mock_run.call_args_list[0]
        cmd = git_add_call[0][0]
        assert cmd[:2] == ["git", "worktree"]
        assert "add" in cmd
        assert "--detach" in cmd
        assert str(expected_wt) in cmd
        assert "origin/main" in cmd

        # Second call: uv sync
        uv_sync_call = mock_run.call_args_list[1]
        cmd = uv_sync_call[0][0]
        assert cmd[0] == "uv"
        assert "sync" in cmd

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_updates_worktree_when_exists(self, mock_run, repo_root: Path):
        """When the worktree directory exists, checkout + clean."""
        mock_run.return_value = subprocess.CompletedProcess([], 0)
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()  # Simulate existing worktree

        result = ensure_e2e_worktree(repo_root)

        assert result == worktree_path

        # First call: git checkout --detach origin/main
        checkout_call = mock_run.call_args_list[0]
        cmd = checkout_call[0][0]
        assert "checkout" in cmd
        assert "--detach" in cmd
        assert "origin/main" in cmd

        # Second call: git clean -fdx --exclude=.venv
        clean_call = mock_run.call_args_list[1]
        cmd = clean_call[0][0]
        assert "clean" in cmd
        assert "--exclude=.venv" in cmd

        # Third call: uv sync
        uv_sync_call = mock_run.call_args_list[2]
        cmd = uv_sync_call[0][0]
        assert cmd[0] == "uv"

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_recovers_on_checkout_failure(self, mock_run, repo_root: Path):
        """When checkout fails, remove and recreate the worktree."""
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()  # Simulate existing worktree

        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            # First call is checkout, which fails
            if call_count == 1:
                assert "checkout" in cmd
                raise subprocess.CalledProcessError(1, cmd, stderr="checkout failed")
            # Remaining calls succeed
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect

        result = ensure_e2e_worktree(repo_root)

        assert result == worktree_path

        # After checkout failure: worktree remove, worktree add, uv sync
        calls = mock_run.call_args_list
        # call 0: checkout (failed)
        # call 1: worktree remove --force
        assert "remove" in calls[1][0][0]
        # call 2: worktree add --detach
        assert "add" in calls[2][0][0]
        # call 3: uv sync
        assert calls[3][0][0][0] == "uv"

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_recovery_removes_stale_non_worktree_directory(self, mock_run, repo_root: Path):
        """When a stale directory exists but is not a registered worktree,
        recovery must rmtree it before git worktree add."""
        worktree_path = get_e2e_worktree_path(repo_root)
        worktree_path.mkdir()
        # Place a file so the directory is non-empty
        (worktree_path / "leftover.txt").write_text("stale")

        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # checkout fails (not a valid worktree)
                raise subprocess.CalledProcessError(1, cmd, stderr="not a git repo")
            if call_count == 2:
                # worktree remove fails (not a registered worktree)
                raise subprocess.CalledProcessError(128, cmd, stderr="not a working tree")
            # worktree add + uv sync succeed
            return subprocess.CompletedProcess(cmd, 0)

        mock_run.side_effect = side_effect

        result = ensure_e2e_worktree(repo_root)

        assert result == worktree_path
        # The stale directory should have been deleted before worktree add
        calls = mock_run.call_args_list
        # call 0: checkout (failed)
        # call 1: worktree remove (failed - not registered)
        # call 2: worktree add (succeeds because rmtree cleared the path)
        assert "add" in calls[2][0][0]
        # call 3: uv sync
        assert calls[3][0][0][0] == "uv"

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_raises_on_git_failure(self, mock_run, repo_root: Path):
        """When git commands fail fatally, raise RuntimeError."""
        mock_run.side_effect = subprocess.CalledProcessError(
            128, ["git", "worktree", "add"], stderr="fatal: not a git repository"
        )

        with pytest.raises(RuntimeError, match="Failed to prepare E2E worktree"):
            ensure_e2e_worktree(repo_root)

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_raises_on_missing_tool(self, mock_run, repo_root: Path):
        """When git or uv is not found, raise RuntimeError."""
        mock_run.side_effect = FileNotFoundError("git not found")

        with pytest.raises(RuntimeError, match="Required tool not found"):
            ensure_e2e_worktree(repo_root)

    @patch("issue_orchestrator.infra.e2e_worktree.subprocess.run")
    def test_uv_sync_uses_frozen_all_extras(self, mock_run, repo_root: Path):
        """Verify uv sync is called with --frozen --all-extras."""
        mock_run.return_value = subprocess.CompletedProcess([], 0)

        ensure_e2e_worktree(repo_root)

        # Find the uv sync call
        uv_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "uv"]
        assert len(uv_calls) == 1
        cmd = uv_calls[0][0][0]
        assert "--frozen" in cmd
        assert "--all-extras" in cmd
