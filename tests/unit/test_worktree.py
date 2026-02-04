"""Unit tests for the worktree module."""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import subprocess

from issue_orchestrator.adapters.worktree._worktree import (
    slugify,
    generate_branch_name,
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    _get_worktree_branch,
    _next_branch_name,
    install_hooks,
    WorktreeError,
)
from issue_orchestrator.ports.worktree_manager import WorktreeReuseOptions


class TestSlugify:
    """Test the slugify function."""

    def test_slugify_basic(self):
        """Test basic slugification."""
        assert slugify("Add user authentication") == "add-user-authentication"

    def test_slugify_special_characters(self):
        """Test slugifying text with special characters."""
        assert slugify("Fix bug in @user's profile!") == "fix-bug-in-user-s-profile"
        assert slugify("Support 100% coverage") == "support-100-coverage"
        assert slugify("Update README.md file") == "update-readme-md-file"

    def test_slugify_multiple_spaces(self):
        """Test slugifying text with multiple consecutive spaces."""
        assert slugify("Add    multiple   spaces") == "add-multiple-spaces"

    def test_slugify_leading_trailing_special_chars(self):
        """Test that leading/trailing hyphens are removed."""
        assert slugify("!!!Important!!!") == "important"
        assert slugify("---dashes---") == "dashes"

    def test_slugify_max_length(self):
        """Test max length truncation."""
        long_text = "This is a very long title that should be truncated"
        result = slugify(long_text, max_length=20)
        assert len(result) <= 20
        assert result == "this-is-a-very-long"

    def test_slugify_max_length_no_trailing_hyphen(self):
        """Test that truncation doesn't leave trailing hyphens."""
        # If truncation happens mid-word, ensure no trailing hyphen
        text = "Add feature for users"
        result = slugify(text, max_length=15)
        assert not result.endswith("-")

    def test_slugify_unicode_characters(self):
        """Test slugifying unicode characters."""
        assert slugify("Café résumé") == "caf-r-sum"
        assert slugify("日本語タイトル") == ""  # Non-latin chars removed
        assert slugify("Fix émoji 🎉 support") == "fix-moji-support"

    def test_slugify_numbers(self):
        """Test slugifying text with numbers."""
        assert slugify("Issue 123 fix") == "issue-123-fix"
        assert slugify("v2.0 release") == "v2-0-release"

    def test_slugify_empty_string(self):
        """Test slugifying empty string."""
        assert slugify("") == ""
        assert slugify("   ") == ""
        assert slugify("!!!") == ""

    def test_slugify_only_special_chars(self):
        """Test text with only special characters."""
        assert slugify("@#$%^&*()") == ""

    def test_slugify_very_long_title(self):
        """Test extremely long title truncation."""
        very_long = "a" * 100
        result = slugify(very_long, max_length=40)
        assert len(result) == 40
        assert result == "a" * 40


class TestGenerateBranchName:
    """Test the generate_branch_name function."""

    def test_generate_branch_name_basic(self):
        """Test basic branch name generation."""
        result = generate_branch_name(123, "Add user authentication")
        assert result == "123-add-user-authentication"

    def test_generate_branch_name_with_special_chars(self):
        """Test branch name with special characters."""
        result = generate_branch_name(456, "Fix bug in @user's profile!")
        assert result == "456-fix-bug-in-user-s-profile"

    def test_generate_branch_name_long_title(self):
        """Test branch name with very long title."""
        long_title = "This is a very long issue title that should be truncated to fit"
        result = generate_branch_name(789, long_title)
        assert result.startswith("789-")
        # Check that the slug part is truncated to max 50 chars
        slug_part = result[4:]  # Remove "789-"
        assert len(slug_part) <= 50

    def test_generate_branch_name_unicode(self):
        """Test branch name with unicode characters."""
        result = generate_branch_name(42, "Add café résumé feature")
        assert result == "42-add-caf-r-sum-feature"

    def test_generate_branch_name_number_formatting(self):
        """Test that issue number is preserved correctly."""
        result = generate_branch_name(1, "Test")
        assert result.startswith("1-")

        result = generate_branch_name(99999, "Test")
        assert result.startswith("99999-")


class TestBranchSuffix:
    """Test branch suffix generation for recreated worktrees."""

    def test_next_branch_name_increments_suffix(self, monkeypatch, tmp_path):
        """Select next available -rN suffix."""
        monkeypatch.setattr(
            "issue_orchestrator.adapters.worktree._worktree._list_branch_names",
            lambda _repo: ["123-fix", "123-fix-r1", "123-fix-r3"],
        )
        assert _next_branch_name(tmp_path, "123-fix") == "123-fix-r4"

    def test_next_branch_name_strips_existing_suffix(self, monkeypatch, tmp_path):
        """Avoid stacking suffixes when branch already has -rN."""
        monkeypatch.setattr(
            "issue_orchestrator.adapters.worktree._worktree._list_branch_names",
            lambda _repo: ["123-fix", "123-fix-r1"],
        )
        assert _next_branch_name(tmp_path, "123-fix-r1") == "123-fix-r2"


class TestCreateWorktree:
    """Test the create_worktree function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_success(self, mock_run, tmp_path):
        """Test successful worktree creation."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"

        def run_side_effect(cmd, *args, **kwargs):
            argv = cmd[3:]
            if argv[:2] == ["worktree", "prune"]:
                return MagicMock(returncode=0, stderr="")
            if argv[:2] == ["worktree", "list"]:
                return MagicMock(returncode=0, stdout="", stderr="")
            if argv[:3] == ["rev-parse", "--verify", "123-add-user-auth"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:3] == ["fetch", "origin", "123-add-user-auth"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:2] == ["symbolic-ref", "refs/remotes/origin/HEAD"]:
                return MagicMock(returncode=1, stderr="")
            if argv[:3] == ["rev-parse", "--verify", "main"]:
                return MagicMock(returncode=0, stdout="main\n", stderr="")
            if argv[:3] == ["fetch", "origin", "main"]:
                return MagicMock(returncode=0, stderr="")
            if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                return MagicMock(returncode=0, stdout="abc123\n", stderr="")
            if argv[:2] == ["worktree", "add"]:
                return MagicMock(returncode=0, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = run_side_effect

        # Execute
        worktree_path, branch_name, *_ = create_worktree(
            repo_root, 123, "Add user auth", worktree_base
        )

        # Verify
        assert branch_name == "123-add-user-auth"
        assert worktree_path == worktree_base / "repo-123"

        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        assert any(cmd[3:5] == ["worktree", "prune"] for cmd in calls)
        assert any(cmd[3:5] == ["worktree", "list"] for cmd in calls)
        assert any(cmd[3:6] == ["fetch", "origin", "main"] for cmd in calls)
        assert any(cmd[3:6] == ["rev-parse", "--verify", "origin/main"] for cmd in calls)

        # Create worktree with new branch (-b flag) from default branch
        worktree_cmd = next(cmd for cmd in calls if cmd[3:5] == ["worktree", "add"])
        assert "-b" in worktree_cmd  # New branch flag
        assert "123-add-user-auth" in worktree_cmd
        assert "origin/main" in worktree_cmd  # Should branch from origin/main

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_default_base(self, mock_run, tmp_path):
        """Test worktree creation with default base directory."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute (no worktree_base specified)
        worktree_path, branch_name, *_ = create_worktree(repo_root, 456, "Fix bug")

        # Verify - should use parent of repo_root as base
        expected_path = tmp_path / "repo-456"
        assert worktree_path == expected_path

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_not_a_git_repo(self, mock_run, tmp_path):
        """Test error when path is not a git repository."""
        # Setup - directory without .git
        repo_root = tmp_path / "not-a-repo"
        repo_root.mkdir()

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Not a git repository"):
            create_worktree(repo_root, 123, "Test")

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.worktree._worktree.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_already_exists(self, mock_run, mock_install_hooks, tmp_path):
        """Test that existing worktree is reused when it passes validation."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        # Create existing worktree directory with valid .git file
        existing_worktree = worktree_base / "repo-123"
        existing_worktree.mkdir()
        # Create .git file to make it look like a valid worktree
        (existing_worktree / ".git").write_text("gitdir: /some/path")

        # Mock subprocess calls for worktree reuse validation:
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            # Prune stale worktrees
            if "prune" in cmd:
                return MagicMock(returncode=0, stderr="")
            # Get current branch
            if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                return MagicMock(returncode=0, stdout="existing-branch\n")
            # Validation: MERGE_HEAD check - return 1 (no merge in progress)
            if "rev-parse" in cmd and "MERGE_HEAD" in cmd:
                return MagicMock(returncode=1, stderr="")
            # Validation: diff --check (no conflicts)
            if "diff" in cmd and "--check" in cmd:
                return MagicMock(returncode=0, stdout="")
            # Pull --rebase to update
            if "pull" in cmd:
                return MagicMock(returncode=0, stderr="")
            # Default success for other commands
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = mock_subprocess

        # Execute - should reuse existing worktree instead of raising error
        path, branch, *_ = create_worktree(repo_root, 123, "Test", worktree_base)

        # Verify it returned the existing worktree
        assert path == existing_worktree
        assert branch == "existing-branch"
        # Verify hooks were reinstalled on reuse
        mock_install_hooks.assert_called_once_with(existing_worktree, None)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_git_command_fails(self, mock_run, tmp_path):
        """Test error when git command fails."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock failed worktree add command
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch (no match)
            MagicMock(returncode=1, stderr=""),  # branch doesn't exist
            MagicMock(returncode=1, stderr=""),  # fetch fails (branch not on remote)
            MagicMock(returncode=1, stderr=""),  # symbolic-ref fails (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # rev-parse main succeeds (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # fetch origin/main succeeds
            MagicMock(returncode=1, stderr="fatal: invalid reference"),  # worktree create fails
        ]

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to create worktree"):
            create_worktree(repo_root, 123, "Test")

    @patch("issue_orchestrator.adapters.worktree._worktree.install_venv_symlink")
    @patch("issue_orchestrator.adapters.worktree._worktree.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_detaches_when_branch_in_use(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        mock_install_venv_symlink,
        tmp_path,
        monkeypatch,
    ):
        """Detach existing worktree branch when reuse is disabled."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        existing_worktree = tmp_path / "worktrees" / "issue-123" / "repo-123"
        existing_worktree.mkdir(parents=True)

        monkeypatch.setenv("ORCHESTRATOR_DISABLE_WORKTREE_REUSE", "1")

        worktree_list_output = (
            f"worktree {existing_worktree}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        # Use a function-based mock that handles various git commands
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "prune" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "worktree" in cmd and "list" in cmd:
                return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
            if "checkout" in cmd and "--detach" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "push" in cmd and "--delete" in cmd:
                return MagicMock(returncode=0, stderr="")
            # Default success for all other commands (worktree add, branch checks, etc)
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = mock_subprocess

        create_worktree(repo_root, 123, "Test")

        assert any(
            call_args[0][0][:4] == ["git", "-C", str(existing_worktree), "checkout"]
            and "--detach" in call_args[0][0]
            for call_args in mock_run.call_args_list
        )
        assert any(
            "worktree" in call_args[0][0]
            for call_args in mock_run.call_args_list
        )

        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()
        mock_install_venv_symlink.assert_called_once()

    @patch("issue_orchestrator.adapters.worktree._worktree.install_venv_symlink")
    @patch("issue_orchestrator.adapters.worktree._worktree.install_claude_settings")
    @patch("issue_orchestrator.adapters.worktree._worktree.install_hooks")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_removes_existing_path_when_reuse_disabled(
        self,
        mock_run,
        mock_install_hooks,
        mock_install_claude_settings,
        mock_install_venv_symlink,
        tmp_path,
        monkeypatch,
    ):
        """Remove existing worktree path when reuse is disabled."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        existing_path = worktree_base / "repo-123"
        existing_path.mkdir()

        monkeypatch.setenv("ORCHESTRATOR_DISABLE_WORKTREE_REUSE", "1")

        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stderr=""),  # worktree remove --force
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch
            MagicMock(returncode=1, stderr=""),  # branch doesn't exist
            MagicMock(returncode=1, stderr=""),  # fetch origin fails
            MagicMock(returncode=1, stderr=""),  # symbolic-ref fails (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # rev-parse main succeeds (get_default_branch)
            MagicMock(returncode=0, stderr=""),  # fetch origin/main succeeds
            MagicMock(returncode=0, stderr=""),  # worktree add
        ]

        create_worktree(repo_root, 123, "Test", worktree_base=worktree_base)

        assert any(
            call_args[0][0][:5] == ["git", "-C", str(repo_root), "worktree", "remove"]
            for call_args in mock_run.call_args_list
        )

        mock_install_hooks.assert_called_once()
        mock_install_claude_settings.assert_called_once()
        mock_install_venv_symlink.assert_called_once()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_creates_base_directory(self, mock_run, tmp_path):
        """Test that worktree base directory is created if it doesn't exist."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "new" / "nested" / "worktrees"

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute
        create_worktree(repo_root, 123, "Test", worktree_base)

        # Verify base directory was created
        assert worktree_base.exists()
        assert worktree_base.is_dir()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_with_complex_title(self, mock_run, tmp_path):
        """Test worktree creation with complex issue title."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute with complex title
        complex_title = "Fix bug in @user's profile (100% coverage) 🎉"
        worktree_path, branch_name, *_ = create_worktree(repo_root, 999, complex_title)

        # Verify branch name is properly slugified
        assert branch_name == "999-fix-bug-in-user-s-profile-100-coverage"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_create_worktree_subprocess_exception(self, mock_run, tmp_path):
        """Test handling of subprocess exceptions."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock: prune succeeds, find worktree (no match), then exception on branch check
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch (no match)
            OSError("Command not found"),  # exception on branch check
        ]

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Error creating worktree"):
            create_worktree(repo_root, 123, "Test")


class TestRemoveWorktree:
    """Test the remove_worktree function."""

    @patch("issue_orchestrator.adapters.worktree._worktree._get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_success(self, mock_run, mock_get_branch, tmp_path):
        """Test successful worktree removal."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        # Mock branch name
        mock_get_branch.return_value = "123-test-branch"

        # Mock successful git commands
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute
        remove_worktree(worktree_path)

        # Verify git commands were called
        assert mock_run.call_count == 2

        # First call: remove worktree
        first_call = mock_run.call_args_list[0][0][0]
        assert first_call[0] == "git"
        assert first_call[1] == "-C"
        assert first_call[2] == str(repo_root)
        assert first_call[3] == "worktree"
        assert first_call[4] == "remove"
        assert first_call[5] == str(worktree_path)

        # Second call: delete branch
        second_call = mock_run.call_args_list[1][0][0]
        assert second_call[0] == "git"
        assert second_call[1] == "-C"
        assert second_call[2] == str(repo_root)
        assert second_call[3] == "branch"
        assert second_call[4] == "-D"
        assert second_call[5] == "123-test-branch"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            remove_worktree(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.worktree._worktree._get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_git_fails(self, mock_run, mock_get_branch, tmp_path):
        """Test error when git worktree remove fails."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: worktree is locked"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to remove worktree"):
            remove_worktree(worktree_path)

    @patch("issue_orchestrator.adapters.worktree._worktree._get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_branch_deletion_fails_silently(
        self, mock_run, mock_get_branch, tmp_path
    ):
        """Test that branch deletion failures don't raise errors."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        mock_get_branch.return_value = "123-test-branch"

        # First call succeeds (remove worktree), second fails (delete branch)
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),
            MagicMock(returncode=1, stderr="error: branch not found"),
        ]

        # Execute - should not raise
        remove_worktree(worktree_path)

        # Verify both commands were attempted
        assert mock_run.call_count == 2

    @patch("issue_orchestrator.adapters.worktree._worktree._get_worktree_branch")
    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_remove_worktree_no_branch_name(self, mock_run, mock_get_branch, tmp_path):
        """Test removal when branch name cannot be determined."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / 'worktree-123'}"
        )

        mock_get_branch.return_value = None

        # Mock successful worktree removal
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute
        remove_worktree(worktree_path)

        # Verify only worktree removal was called (not branch deletion)
        assert mock_run.call_count == 1


class TestFindWorktreeForBranch:
    """Test the find_worktree_for_branch function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_found(self, mock_run, tmp_path):
        """Test finding an existing worktree for a branch."""
        from issue_orchestrator.adapters.worktree._worktree import find_worktree_for_branch

        mock_output = """worktree /path/to/main
HEAD abc123
branch refs/heads/main

worktree /path/to/worktree-128
HEAD def456
branch refs/heads/128-m9-ios-styling

"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        result = find_worktree_for_branch(tmp_path, "128-m9-ios-styling")
        assert result == Path("/path/to/worktree-128")

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_not_found(self, mock_run, tmp_path):
        """Test when branch is not checked out in any worktree."""
        from issue_orchestrator.adapters.worktree._worktree import find_worktree_for_branch

        mock_output = """worktree /path/to/main
HEAD abc123
branch refs/heads/main

"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        result = find_worktree_for_branch(tmp_path, "nonexistent-branch")
        assert result is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_find_worktree_for_branch_git_fails(self, mock_run, tmp_path):
        """Test when git command fails."""
        from issue_orchestrator.adapters.worktree._worktree import find_worktree_for_branch

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = find_worktree_for_branch(tmp_path, "some-branch")
        assert result is None


class TestListWorktrees:
    """Test the list_worktrees function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_success(self, mock_run):
        """Test successful listing of worktrees."""
        # Mock git output
        mock_output = """worktree /path/to/main
branch refs/heads/main

worktree /path/to/worktree-123
branch refs/heads/123-feature

worktree /path/to/worktree-456
branch refs/heads/456-bugfix
"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        worktrees = list_worktrees(Path("/tmp/repo"))

        # Verify
        assert len(worktrees) == 3
        assert Path("/path/to/main") in worktrees
        assert Path("/path/to/worktree-123") in worktrees
        assert Path("/path/to/worktree-456") in worktrees

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_empty(self, mock_run):
        """Test listing when only main worktree exists."""
        # Mock git output with only main worktree
        mock_output = """worktree /path/to/main
branch refs/heads/main
"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        worktrees = list_worktrees(Path("/tmp/repo"))

        # Verify
        assert len(worktrees) == 1
        assert Path("/path/to/main") in worktrees

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_git_fails(self, mock_run):
        """Test error when git command fails."""
        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list worktrees"):
            list_worktrees(Path("/tmp/repo"))

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_list_worktrees_subprocess_exception(self, mock_run):
        """Test handling of subprocess exceptions."""
        # Mock subprocess exception
        mock_run.side_effect = OSError("Command not found")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Error listing worktrees"):
            list_worktrees(Path("/tmp/repo"))


class TestWorktreeExists:
    """Test the worktree_exists function."""

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_true(self, mock_list):
        """Test checking existing worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-123"), Path("/tmp/repo"))

        # Verify
        assert result is True

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_false(self, mock_list):
        """Test checking non-existent worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-999"), Path("/tmp/repo"))

        # Verify
        assert result is False

    @patch("issue_orchestrator.adapters.worktree._worktree.list_worktrees")
    def test_worktree_exists_error_propagates(self, mock_list):
        """Test that errors from list_worktrees propagate."""
        # Mock error from list_worktrees
        mock_list.side_effect = WorktreeError("Failed to list")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list"):
            worktree_exists(Path("/path/to/worktree"), Path("/tmp/repo"))


class TestHasUncommittedChanges:
    """Test the has_uncommitted_changes function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_true(self, mock_run, tmp_path):
        """Test detecting uncommitted changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with changes
        mock_output = " M file1.txt\n?? file2.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify
        assert result is True

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_false(self, mock_run, tmp_path):
        """Test no uncommitted changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with no changes
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify
        assert result is False

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            has_uncommitted_changes(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_git_fails(self, mock_run, tmp_path):
        """Test error when git status fails."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to check worktree status"):
            has_uncommitted_changes(worktree_path)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_staged_only(self, mock_run, tmp_path):
        """Test detecting staged changes."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with staged changes
        mock_output = "M  file1.txt\nA  file2.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify - staged changes count as uncommitted
        assert result is True

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_has_uncommitted_changes_untracked_only(self, mock_run, tmp_path):
        """Test detecting untracked files."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock git status output with untracked files
        mock_output = "?? new_file.txt\n"
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        result = has_uncommitted_changes(worktree_path)

        # Verify - untracked files count as uncommitted
        assert result is True


class TestGetWorktreeBranch:
    """Test the _get_worktree_branch helper function."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_success(self, mock_run, tmp_path):
        """Test successfully getting branch name."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock git output
        mock_run.return_value = MagicMock(
            returncode=0, stdout="123-feature-branch\n", stderr=""
        )

        # Execute
        branch_name = _get_worktree_branch(worktree_path)

        # Verify
        assert branch_name == "123-feature-branch"

        # Check git command
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"
        assert cmd[1] == "-C"
        assert cmd[2] == str(worktree_path)
        assert cmd[3] == "rev-parse"
        assert cmd[4] == "--abbrev-ref"
        assert cmd[5] == "HEAD"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_git_fails(self, mock_run, tmp_path):
        """Test when git command fails."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute
        branch_name = _get_worktree_branch(worktree_path)

        # Verify - should return None on failure
        assert branch_name is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_empty_output(self, mock_run, tmp_path):
        """Test when git returns empty output."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock empty git output
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Execute
        branch_name = _get_worktree_branch(worktree_path)

        # Verify
        assert branch_name is None

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_get_worktree_branch_subprocess_exception(self, mock_run, tmp_path):
        """Test handling of subprocess exceptions."""
        # Setup
        worktree_path = tmp_path / "worktree-123"

        # Mock subprocess exception
        mock_run.side_effect = OSError("Command not found")

        # Execute
        branch_name = _get_worktree_branch(worktree_path)

        # Verify - should return None on exception
        assert branch_name is None


class TestIntegrationScenarios:
    """Test integration scenarios with multiple operations."""

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_full_lifecycle(self, mock_run, tmp_path):
        """Test complete lifecycle: create, check, remove."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"

        # Mock all git commands to succeed
        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "worktree" in cmd and "add" in cmd:
                return MagicMock(returncode=0, stderr="")
            elif "status" in cmd and "--porcelain" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "rev-parse" in cmd:
                return MagicMock(returncode=0, stdout="123-test-feature\n", stderr="")
            elif "worktree" in cmd and "remove" in cmd:
                return MagicMock(returncode=0, stderr="")
            elif "branch" in cmd and "-D" in cmd:
                return MagicMock(returncode=0, stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = mock_git_command

        # Create worktree
        worktree_path, branch_name, *_ = create_worktree(
            repo_root, 123, "Test feature", worktree_base
        )

        assert branch_name == "123-test-feature"
        assert worktree_path == worktree_base / "repo-123"

        # Check for uncommitted changes (should be clean)
        worktree_path.mkdir(parents=True, exist_ok=True)  # Create for existence check
        (worktree_path / ".git").write_text(
            f"gitdir: {repo_root / '.git' / 'worktrees' / worktree_path.name}"
        )
        result = has_uncommitted_changes(worktree_path)
        assert result is False

        # Remove worktree
        remove_worktree(worktree_path)

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_edge_case_titles(self, mock_run, tmp_path):
        """Test various edge case issue titles."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        mock_run.return_value = MagicMock(returncode=0, stderr="")

        edge_cases = [
            (1, "!!!URGENT!!!", "1-urgent"),
            (2, "日本語タイトル", "2-"),  # Non-latin chars
            (3, "Add @mention & #hashtag", "3-add-mention-hashtag"),
            (4, "Fix 100% coverage", "4-fix-100-coverage"),
            (5, "a" * 100, "5-" + "a" * 50),  # Very long
            (6, "   spaces   ", "6-spaces"),
            (7, "kebab-case-title", "7-kebab-case-title"),
            (8, "CamelCaseTitle", "8-camelcasetitle"),
        ]

        for issue_num, title, expected_branch in edge_cases:
            worktree_path, branch_name, *_ = create_worktree(
                repo_root, issue_num, title, tmp_path / "worktrees"
            )
            assert branch_name == expected_branch


class TestInstallHooks:
    """Test the install_hooks function including hook chaining."""

    def test_install_hooks_no_git_file(self, tmp_path):
        """Test that install_hooks does nothing if .git file doesn't exist."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        # No .git file
        
        # Should not raise, just return
        install_hooks(worktree_path)

    def test_install_hooks_invalid_git_file(self, tmp_path):
        """Test that install_hooks handles invalid .git file content."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text("invalid content")
        
        # Should not raise, just return
        install_hooks(worktree_path)

    def test_install_hooks_no_project_hook(self, tmp_path):
        """Test installing hooks when project has no pre-push hook."""
        # Setup fake git structure
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        main_hooks = main_git / "hooks"
        main_hooks.mkdir()
        # No pre-push hook in main repo
        
        worktrees_dir = main_git / "worktrees" / "test-worktree"
        worktrees_dir.mkdir(parents=True)
        hooks_dir = worktrees_dir / "hooks"
        
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(f"gitdir: {worktrees_dir}")
        
        # Create a fake orchestrator hook
        from issue_orchestrator.adapters.worktree._worktree import HOOKS_DIR
        
        install_hooks(worktree_path)
        
        # Should have installed orchestrator's hook directly (no chaining)
        pre_push = hooks_dir / "pre-push"
        assert pre_push.exists()
        # Should NOT have project or orchestrator suffixed hooks
        assert not (hooks_dir / "pre-push.project").exists()
        assert not (hooks_dir / "pre-push.orchestrator").exists()

    def test_install_hooks_chains_with_project_hook(self, tmp_path):
        """Test that hooks are chained when project has a pre-push hook."""
        # Setup fake git structure
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        main_hooks = main_git / "hooks"
        main_hooks.mkdir()
        
        # Create a project pre-push hook
        project_hook = main_hooks / "pre-push"
        project_hook.write_text("#!/bin/bash\necho 'Project hook'\nexit 0\n")
        project_hook.chmod(0o755)
        
        worktrees_dir = main_git / "worktrees" / "test-worktree"
        worktrees_dir.mkdir(parents=True)
        hooks_dir = worktrees_dir / "hooks"
        
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(f"gitdir: {worktrees_dir}")
        
        install_hooks(worktree_path)
        
        # Verify chained hooks were created
        pre_push = hooks_dir / "pre-push"
        pre_push_project = hooks_dir / "pre-push.project"
        pre_push_orchestrator = hooks_dir / "pre-push.orchestrator"
        
        assert pre_push.exists(), "Wrapper hook should exist"
        assert pre_push_project.exists(), "Project hook copy should exist"
        assert pre_push_orchestrator.exists(), "Orchestrator hook copy should exist"
        
        # Verify wrapper content chains both hooks
        wrapper_content = pre_push.read_text()
        assert "pre-push.project" in wrapper_content, "Wrapper should call project hook"
        assert "pre-push.orchestrator" in wrapper_content, "Wrapper should call orchestrator hook"
        assert "set -e" in wrapper_content, "Wrapper should fail on error"
        
        # Verify project hook was copied correctly
        assert "Project hook" in pre_push_project.read_text()
        
        # Verify all hooks are executable
        assert pre_push.stat().st_mode & 0o111, "Wrapper should be executable"
        assert pre_push_project.stat().st_mode & 0o111, "Project hook should be executable"
        assert pre_push_orchestrator.stat().st_mode & 0o111, "Orchestrator hook should be executable"

    @patch("issue_orchestrator.adapters.git.git_cli.subprocess.run")
    def test_install_hooks_with_custom_hooks_path(self, mock_run, tmp_path):
        """Test hook installation when project uses core.hooksPath (e.g., .githooks/)."""
        # Setup fake git structure
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        main_hooks = main_git / "hooks"
        main_hooks.mkdir()

        worktrees_dir = main_git / "worktrees" / "test-worktree"
        worktrees_dir.mkdir(parents=True)
        hooks_dir = worktrees_dir / "hooks"

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()
        (worktree_path / ".git").write_text(f"gitdir: {worktrees_dir}")

        # Create .githooks directory with project hook in MAIN REPO (simulating version-controlled hooks)
        # The hook should be in the main repo, not the worktree - worktrees share the main repo's hooks
        custom_hooks_dir = main_repo / ".githooks"
        custom_hooks_dir.mkdir()
        project_hook = custom_hooks_dir / "pre-push"
        project_hook.write_text("#!/bin/bash\necho 'Custom hooks path hook'\nexit 0\n")
        project_hook.chmod(0o755)

        # Mock git config to return custom hooksPath
        def mock_git_command(*args, **kwargs):
            cmd = args[0]
            if "config" in cmd and "--get" in cmd and "core.hooksPath" in cmd:
                return MagicMock(returncode=0, stdout=".githooks\n", stderr="")
            elif "config" in cmd and "extensions.worktreeConfig" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            elif "config" in cmd and "--worktree" in cmd and "core.hooksPath" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="")

        mock_run.side_effect = mock_git_command

        install_hooks(worktree_path)

        # Verify chained hooks were created in gitdir/hooks (not .githooks)
        pre_push = hooks_dir / "pre-push"
        pre_push_project = hooks_dir / "pre-push.project"
        pre_push_orchestrator = hooks_dir / "pre-push.orchestrator"

        assert pre_push.exists(), "Wrapper hook should exist in gitdir/hooks"
        assert pre_push_project.exists(), "Project hook copy should exist"
        assert pre_push_orchestrator.exists(), "Orchestrator hook copy should exist"

        # Verify git config was called to enable worktreeConfig extension
        worktree_config_calls = [call for call in mock_run.call_args_list
                                 if "config" in str(call) and "extensions.worktreeConfig" in str(call)]
        assert len(worktree_config_calls) >= 1, "Should have enabled worktreeConfig extension"

        # Verify git config was called to override hooksPath for this worktree only
        config_calls = [call for call in mock_run.call_args_list
                        if "config" in str(call) and "--worktree" in str(call)]
        assert len(config_calls) >= 1, "Should have set worktree-specific hooksPath config"

        # Verify project hook was copied from custom hooks path
        assert "Custom hooks path hook" in pre_push_project.read_text()


class TestInstallClaudeSettings:
    """Tests for install_claude_settings function."""

    def test_install_claude_settings_creates_file(self, tmp_path):
        """Test that install_claude_settings creates .claude/settings.json."""
        from issue_orchestrator.adapters.worktree._worktree import install_claude_settings

        install_claude_settings(tmp_path)

        settings_file = tmp_path / ".claude" / "settings.json"
        assert settings_file.exists()

    def test_install_claude_settings_has_stop_hook(self, tmp_path):
        """Test that the settings contain a Stop hook."""
        import json
        from issue_orchestrator.adapters.worktree._worktree import install_claude_settings

        install_claude_settings(tmp_path)

        settings_file = tmp_path / ".claude" / "settings.json"
        settings = json.loads(settings_file.read_text())

        assert "hooks" in settings
        assert "Stop" in settings["hooks"]
        assert len(settings["hooks"]["Stop"]) > 0

    def test_install_claude_settings_merges_with_existing(self, tmp_path):
        """Test that install_claude_settings merges with existing settings."""
        import json
        from issue_orchestrator.adapters.worktree._worktree import install_claude_settings

        # Create existing settings
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_file = claude_dir / "settings.json"
        existing = {"some_key": "some_value", "hooks": {"PreToolUse": []}}
        settings_file.write_text(json.dumps(existing))

        install_claude_settings(tmp_path)

        settings = json.loads(settings_file.read_text())

        # Original content preserved
        assert settings["some_key"] == "some_value"
        assert "PreToolUse" in settings["hooks"]
        # New hook added
        assert "Stop" in settings["hooks"]


class TestInstallVenvSymlink:
    """Tests for install_venv_symlink function."""

    def test_creates_symlink_when_venv_exists(self, tmp_path):
        """Test that symlink is created when main repo has .venv."""
        from issue_orchestrator.adapters.worktree._worktree import install_venv_symlink

        # Setup main repo with .venv
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_venv = main_repo / ".venv"
        main_venv.mkdir()

        # Setup worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Execute
        result = install_venv_symlink(worktree, main_repo)

        # Verify
        assert result is True
        worktree_venv = worktree / ".venv"
        assert worktree_venv.is_symlink()
        assert worktree_venv.resolve() == main_venv

    def test_returns_false_when_no_main_venv(self, tmp_path):
        """Test that function returns False when main repo has no .venv."""
        from issue_orchestrator.adapters.worktree._worktree import install_venv_symlink

        # Setup main repo without .venv
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()

        # Setup worktree
        worktree = tmp_path / "worktree"
        worktree.mkdir()

        # Execute
        result = install_venv_symlink(worktree, main_repo)

        # Verify
        assert result is False
        assert not (worktree / ".venv").exists()

    def test_skips_if_venv_already_exists(self, tmp_path):
        """Test that existing .venv in worktree is not overwritten."""
        from issue_orchestrator.adapters.worktree._worktree import install_venv_symlink

        # Setup main repo with .venv
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_venv = main_repo / ".venv"
        main_venv.mkdir()

        # Setup worktree with existing .venv (real directory)
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        worktree_venv = worktree / ".venv"
        worktree_venv.mkdir()
        (worktree_venv / "marker.txt").write_text("existing")

        # Execute
        result = install_venv_symlink(worktree, main_repo)

        # Verify - should return True but not overwrite
        assert result is True
        assert not worktree_venv.is_symlink()  # Still a real directory
        assert (worktree_venv / "marker.txt").exists()  # Content preserved

    def test_skips_if_symlink_already_exists(self, tmp_path):
        """Test that existing symlink is not replaced."""
        from issue_orchestrator.adapters.worktree._worktree import install_venv_symlink

        # Setup main repo with .venv
        main_repo = tmp_path / "main_repo"
        main_repo.mkdir()
        main_venv = main_repo / ".venv"
        main_venv.mkdir()

        # Setup another target for existing symlink
        other_venv = tmp_path / "other_venv"
        other_venv.mkdir()

        # Setup worktree with existing symlink to other_venv
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        worktree_venv = worktree / ".venv"
        worktree_venv.symlink_to(other_venv)

        # Execute
        result = install_venv_symlink(worktree, main_repo)

        # Verify - should return True but not change existing symlink
        assert result is True
        assert worktree_venv.is_symlink()
        assert worktree_venv.resolve() == other_venv  # Still points to other_venv


class TestCreateWorktreeReuse:
    """Test reuse flow via create_worktree (public API)."""

    def _policy(self):
        from issue_orchestrator.ports.worktree_policy import ValidationResult, SyncResult

        class AlwaysReusePolicy:
            def validate_for_reuse(self, worktree_path, expected_branch, repo_root):
                return ValidationResult(can_reuse=True, reason="ok")

            def sync_remote_refs(self, worktree_path, branch_name):
                return SyncResult(success=True)

            def delete_worktree(self, worktree_path, repo_root):
                return True

        return AlwaysReusePolicy()

    def test_reuse_rebases_onto_origin_main(self, tmp_path):
        """Verify reuse path rebases onto origin/main."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_claude_settings"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_venv_symlink"),
            patch("issue_orchestrator.adapters.worktree._worktree.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["reset", "--hard"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            worktree_path_out, branch_name, reuse_status, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert reuse_status == "reused"
            assert worktree_path_out == worktree_path
            assert branch_name == "123-test"
            assert uncommitted == 0
            assert commits == 0
            rebase_call = next(
                call_args[0][0]
                for call_args in mock_run.call_args_list
                if call_args[0][0][3:5] == ["rebase", "origin/main"]
            )
            assert "rebase" in rebase_call
            assert "origin/main" in rebase_call

    def test_reuse_rebase_conflict_discards_commits(self, tmp_path):
        """Verify reuse path resets to origin/main on rebase failure."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_claude_settings"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_venv_symlink"),
            patch("issue_orchestrator.adapters.worktree._worktree.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "HEAD":
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=1, stdout="", stderr="CONFLICT")
                if argv[:2] == ["rebase", "--abort"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-list", "--count", "origin/main..HEAD"]:
                    return MagicMock(returncode=0, stdout="2\n", stderr="")
                if argv[:2] == ["reset", "--hard"] and len(argv) == 3 and argv[2] == "origin/main":
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            _, _, _, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert uncommitted == 0
            assert commits == 2

    def test_reuse_counts_uncommitted_changes(self, tmp_path):
        """Verify reuse path counts discarded uncommitted changes."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        worktree_list_output = (
            f"worktree {worktree_path}\n"
            "HEAD abc123\n"
            "branch refs/heads/123-test\n\n"
        )

        with (
            patch("issue_orchestrator.adapters.git.git_cli.subprocess.run") as mock_run,
            patch("issue_orchestrator.adapters.worktree._worktree.install_hooks"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_claude_settings"),
            patch("issue_orchestrator.adapters.worktree._worktree.install_venv_symlink"),
            patch("issue_orchestrator.adapters.worktree._worktree.sync_cli_tools"),
        ):
            def run_side_effect(cmd, *args, **kwargs):
                argv = cmd[3:]
                if argv[:2] == ["worktree", "prune"]:
                    return MagicMock(returncode=0, stderr="")
                if argv[:2] == ["worktree", "list"]:
                    return MagicMock(returncode=0, stdout=worktree_list_output, stderr="")
                if argv[:3] == ["fetch", "origin", "main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:3] == ["rev-parse", "--verify", "origin/main"]:
                    return MagicMock(returncode=0, stdout="abc123\n", stderr="")
                if argv[:3] == ["rev-parse", "--abbrev-ref", "HEAD"]:
                    return MagicMock(returncode=0, stdout="123-test\n", stderr="")
                if argv[:2] == ["status", "--porcelain"]:
                    return MagicMock(returncode=0, stdout="M file1.txt\nM file2.txt\n", stderr="")
                if argv[:2] == ["reset", "--hard"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["clean", "-fd"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if argv[:2] == ["rebase", "origin/main"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = run_side_effect

            _, _, _, _, _, uncommitted, commits = create_worktree(
                repo_root,
                123,
                "Test",
                worktree_base=tmp_path,
                branch_name="123-test",
                base_branch="main",
                reuse_options=WorktreeReuseOptions(reuse_push_preflight=False),
                policy=self._policy(),
            )

            assert uncommitted == 2
            assert commits == 0


# =============================================================================
# Worktree Preparation Tests (control/worktree.py)
# =============================================================================

from issue_orchestrator.control.worktree import Worktree, WorktreePreparationError
from issue_orchestrator.ports.session_output import SessionOutput
import json


@pytest.fixture
def worktree_dir(tmp_path: Path) -> Path:
    """Create a temporary worktree directory with orchestrator dir."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    orchestrator_dir = worktree / ".issue-orchestrator"
    orchestrator_dir.mkdir()
    return worktree


@pytest.fixture
def mock_session_output() -> MagicMock:
    """Create a mock SessionOutput for testing."""
    return MagicMock(spec=SessionOutput)


@pytest.fixture
def worktree(worktree_dir: Path, mock_session_output: MagicMock) -> Worktree:
    """Create a Worktree instance for testing."""
    return Worktree(worktree_dir, issue_number=123, session_output=mock_session_output)


class TestWorktreePrepareForSession:
    """Tests for Worktree.prepare_for_session()."""

    def test_removes_completion_files(self, worktree: Worktree, worktree_dir: Path):
        """Removes completion.json files."""
        completion = worktree_dir / ".issue-orchestrator" / "completion.json"
        completion.write_text(json.dumps({
            "session_id": "old-session",
            "timestamp": "2026-01-01T00:00:00",
            "outcome": "completed",
            "summary": "Old",
        }))

        assert completion.exists()
        worktree.prepare_for_session("new-session")
        assert not completion.exists()

    def test_keeps_session_identity_files(self, worktree: Worktree, worktree_dir: Path):
        """Keeps per-session identity files for recent runs."""
        identity = worktree_dir / ".issue-orchestrator" / "sessions" / "new-session" / "identity.json"
        identity.parent.mkdir(parents=True, exist_ok=True)
        identity.write_text(json.dumps({"session_name": "old"}))

        assert identity.exists()
        worktree.prepare_for_session("new-session")
        assert identity.exists()

    def test_removes_multiple_completion_files(self, worktree: Worktree, worktree_dir: Path):
        """Removes all completion*.json files."""
        orch_dir = worktree_dir / ".issue-orchestrator"

        files = [
            "completion.json",
            "completion-agent_backend.json",
            "completion-agent_e2e-test.json",
        ]
        for name in files:
            (orch_dir / name).write_text(json.dumps({
                "session_id": "old",
                "timestamp": "2026-01-01T00:00:00",
                "outcome": "completed",
                "summary": "Test",
            }))

        worktree.prepare_for_session("new-session")

        for name in files:
            assert not (orch_dir / name).exists()

    def test_keeps_pane_log(self, worktree: Worktree, worktree_dir: Path):
        """Keeps pane.log for recent runs."""
        pane_log = worktree_dir / ".issue-orchestrator" / "sessions" / "new-session" / "pane.log"
        pane_log.parent.mkdir(parents=True, exist_ok=True)
        pane_log.write_text("Old session output from Claude Code\n")

        assert pane_log.exists()
        worktree.prepare_for_session("new-session")
        assert pane_log.exists()

    def test_no_error_when_orchestrator_dir_missing(self, tmp_path: Path, mock_session_output: MagicMock):
        """No error when .issue-orchestrator dir doesn't exist."""
        worktree = Worktree(tmp_path, issue_number=123, session_output=mock_session_output)
        worktree.prepare_for_session("new-session")  # Should not raise

    def test_prunes_old_session_runs(self, tmp_path: Path, mock_session_output: MagicMock):
        """Calls session_output.prune_runs with correct arguments."""
        # Configure mock to return empty list (no pruning needed)
        mock_session_output.prune_runs.return_value = []

        worktree = Worktree(tmp_path, issue_number=123, retain_runs=2, session_output=mock_session_output)
        worktree.prepare_for_session("issue-1")

        # Verify prune_runs was called with correct path and retention
        mock_session_output.prune_runs.assert_called_once_with(tmp_path, 2)

    def test_raises_worktree_preparation_error_on_delete_failure(
        self, worktree: Worktree, worktree_dir: Path, monkeypatch
    ):
        """Raises WorktreePreparationError if file cannot be deleted."""
        orch_dir = worktree_dir / ".issue-orchestrator"
        completion = orch_dir / "completion.json"
        completion.write_text("{}")

        def _raise_unlink(_self):
            raise OSError("Permission denied")

        monkeypatch.setattr(Path, "unlink", _raise_unlink)

        with pytest.raises(WorktreePreparationError) as exc_info:
            worktree.prepare_for_session("new-session")
        # Verify exception properties
        assert exc_info.value.path == worktree_dir
        assert exc_info.value.issue_number == 123
        assert "Cannot delete stale files" in str(exc_info.value)
        # Verify OSError is chained
        assert exc_info.value.__cause__ is not None
        assert isinstance(exc_info.value.__cause__, OSError)


class TestWorktreePreparationError:
    """Tests for the WorktreePreparationError exception."""

    def test_exception_properties(self, tmp_path: Path):
        """Test that exception stores path and issue_number."""
        error = WorktreePreparationError(
            tmp_path, 456, "Test error message"
        )
        assert error.path == tmp_path
        assert error.issue_number == 456
        assert str(error) == "Test error message"

    def test_exception_is_raised_with_oserror_cause(self, tmp_path: Path):
        """Test exception can be chained with OSError."""
        original = OSError("Permission denied")
        error = WorktreePreparationError(
            tmp_path, 789, "Cannot delete file"
        )
        error.__cause__ = original

        assert error.__cause__ is original
