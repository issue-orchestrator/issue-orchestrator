"""Unit tests for the worktree module."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import subprocess

from issue_orchestrator.worktree import (
    slugify,
    generate_branch_name,
    create_worktree,
    remove_worktree,
    list_worktrees,
    worktree_exists,
    has_uncommitted_changes,
    _get_worktree_branch,
    install_hooks,
    WorktreeError,
)


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


class TestCreateWorktree:
    """Test the create_worktree function."""

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_create_worktree_success(self, mock_run, tmp_path):
        """Test successful worktree creation."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        worktree_base = tmp_path / "worktrees"

        # Mock: prune, find existing worktree, check if branch exists, create worktree
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr=""),  # prune succeeds
            MagicMock(returncode=0, stdout="", stderr=""),  # find_worktree_for_branch (no match)
            MagicMock(returncode=1, stderr=""),  # branch doesn't exist
            MagicMock(returncode=0, stderr=""),  # worktree create succeeds
        ]

        # Execute
        worktree_path, branch_name = create_worktree(
            repo_root, 123, "Add user auth", worktree_base
        )

        # Verify
        assert branch_name == "123-add-user-auth"
        assert worktree_path == worktree_base / "repo-123"

        # Check git commands were called correctly
        assert mock_run.call_count == 4

        # First call: prune stale worktrees
        prune_cmd = mock_run.call_args_list[0][0][0]
        assert prune_cmd[:3] == ["git", "-C", str(repo_root)]
        assert "prune" in prune_cmd

        # Second call: find worktree for branch
        find_cmd = mock_run.call_args_list[1][0][0]
        assert "worktree" in find_cmd and "list" in find_cmd

        # Third call: check if branch exists
        branch_check_cmd = mock_run.call_args_list[2][0][0]
        assert branch_check_cmd[:3] == ["git", "-C", str(repo_root)]
        assert "rev-parse" in branch_check_cmd

        # Fourth call: create worktree with new branch (-b flag)
        worktree_cmd = mock_run.call_args_list[3][0][0]
        assert worktree_cmd[0] == "git"
        assert worktree_cmd[3] == "worktree"
        assert worktree_cmd[4] == "add"
        assert "-b" in worktree_cmd  # New branch flag
        assert "123-add-user-auth" in worktree_cmd

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_create_worktree_default_base(self, mock_run, tmp_path):
        """Test worktree creation with default base directory."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock successful git command
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute (no worktree_base specified)
        worktree_path, branch_name = create_worktree(repo_root, 456, "Fix bug")

        # Verify - should use parent of repo_root as base
        expected_path = tmp_path / "repo-456"
        assert worktree_path == expected_path

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.install_hooks")
    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_create_worktree_already_exists(self, mock_run, mock_install_hooks, tmp_path):
        """Test error when worktree path already exists."""
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

        # Mock subprocess calls:
        # 1. prune call
        # 2. rev-parse to get current branch
        # 3. pull --rebase
        def mock_subprocess(*args, **kwargs):
            cmd = args[0]
            if "prune" in cmd:
                return MagicMock(returncode=0, stderr="")
            if "rev-parse" in cmd and "--abbrev-ref" in cmd:
                return MagicMock(returncode=0, stdout="existing-branch\n")
            if "pull" in cmd:
                return MagicMock(returncode=0, stderr="")
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = mock_subprocess

        # Execute - should reuse existing worktree instead of raising error
        path, branch = create_worktree(repo_root, 123, "Test", worktree_base)

        # Verify it returned the existing worktree
        assert path == existing_worktree
        assert branch == "existing-branch"
        # Verify hooks were reinstalled on reuse
        mock_install_hooks.assert_called_once_with(existing_worktree, None)

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_create_worktree_git_command_fails(self, mock_run, tmp_path):
        """Test error when git command fails."""
        # Setup
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: invalid reference"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to create worktree"):
            create_worktree(repo_root, 123, "Test")

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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
        worktree_path, branch_name = create_worktree(repo_root, 999, complex_title)

        # Verify branch name is properly slugified
        assert branch_name == "999-fix-bug-in-user-s-profile-100-coverage"

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree._get_worktree_branch")
    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_remove_worktree_success(self, mock_run, mock_get_branch, tmp_path):
        """Test successful worktree removal."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

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
        assert first_call[1] == "worktree"
        assert first_call[2] == "remove"
        assert first_call[3] == str(worktree_path)

        # Second call: delete branch
        second_call = mock_run.call_args_list[1][0][0]
        assert second_call[0] == "git"
        assert second_call[1] == "branch"
        assert second_call[2] == "-D"
        assert second_call[3] == "123-test-branch"

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_remove_worktree_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            remove_worktree(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.worktree._get_worktree_branch")
    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_remove_worktree_git_fails(self, mock_run, mock_get_branch, tmp_path):
        """Test error when git worktree remove fails."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: worktree is locked"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to remove worktree"):
            remove_worktree(worktree_path)

    @patch("issue_orchestrator.worktree._get_worktree_branch")
    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_remove_worktree_branch_deletion_fails_silently(
        self, mock_run, mock_get_branch, tmp_path
    ):
        """Test that branch deletion failures don't raise errors."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

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

    @patch("issue_orchestrator.worktree._get_worktree_branch")
    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_remove_worktree_no_branch_name(self, mock_run, mock_get_branch, tmp_path):
        """Test removal when branch name cannot be determined."""
        # Setup
        worktree_path = tmp_path / "worktree-123"
        worktree_path.mkdir()

        mock_get_branch.return_value = None

        # Mock successful worktree removal
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # Execute
        remove_worktree(worktree_path)

        # Verify only worktree removal was called (not branch deletion)
        assert mock_run.call_count == 1


class TestFindWorktreeForBranch:
    """Test the find_worktree_for_branch function."""

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_find_worktree_for_branch_found(self, mock_run, tmp_path):
        """Test finding an existing worktree for a branch."""
        from issue_orchestrator.worktree import find_worktree_for_branch

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

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_find_worktree_for_branch_not_found(self, mock_run, tmp_path):
        """Test when branch is not checked out in any worktree."""
        from issue_orchestrator.worktree import find_worktree_for_branch

        mock_output = """worktree /path/to/main
HEAD abc123
branch refs/heads/main

"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        result = find_worktree_for_branch(tmp_path, "nonexistent-branch")
        assert result is None

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_find_worktree_for_branch_git_fails(self, mock_run, tmp_path):
        """Test when git command fails."""
        from issue_orchestrator.worktree import find_worktree_for_branch

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        result = find_worktree_for_branch(tmp_path, "some-branch")
        assert result is None


class TestListWorktrees:
    """Test the list_worktrees function."""

    @patch("issue_orchestrator.worktree.subprocess.run")
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
        worktrees = list_worktrees()

        # Verify
        assert len(worktrees) == 3
        assert Path("/path/to/main") in worktrees
        assert Path("/path/to/worktree-123") in worktrees
        assert Path("/path/to/worktree-456") in worktrees

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_list_worktrees_empty(self, mock_run):
        """Test listing when only main worktree exists."""
        # Mock git output with only main worktree
        mock_output = """worktree /path/to/main
branch refs/heads/main
"""
        mock_run.return_value = MagicMock(returncode=0, stdout=mock_output, stderr="")

        # Execute
        worktrees = list_worktrees()

        # Verify
        assert len(worktrees) == 1
        assert Path("/path/to/main") in worktrees

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_list_worktrees_git_fails(self, mock_run):
        """Test error when git command fails."""
        # Mock failed git command
        mock_run.return_value = MagicMock(
            returncode=1, stderr="fatal: not a git repository"
        )

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list worktrees"):
            list_worktrees()

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_list_worktrees_subprocess_exception(self, mock_run):
        """Test handling of subprocess exceptions."""
        # Mock subprocess exception
        mock_run.side_effect = OSError("Command not found")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Error listing worktrees"):
            list_worktrees()


class TestWorktreeExists:
    """Test the worktree_exists function."""

    @patch("issue_orchestrator.worktree.list_worktrees")
    def test_worktree_exists_true(self, mock_list):
        """Test checking existing worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-123"))

        # Verify
        assert result is True

    @patch("issue_orchestrator.worktree.list_worktrees")
    def test_worktree_exists_false(self, mock_list):
        """Test checking non-existent worktree."""
        # Mock list of worktrees
        mock_list.return_value = [
            Path("/path/to/main"),
            Path("/path/to/worktree-123"),
        ]

        # Execute
        result = worktree_exists(Path("/path/to/worktree-999"))

        # Verify
        assert result is False

    @patch("issue_orchestrator.worktree.list_worktrees")
    def test_worktree_exists_error_propagates(self, mock_list):
        """Test that errors from list_worktrees propagate."""
        # Mock error from list_worktrees
        mock_list.side_effect = WorktreeError("Failed to list")

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Failed to list"):
            worktree_exists(Path("/path/to/worktree"))


class TestHasUncommittedChanges:
    """Test the has_uncommitted_changes function."""

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
    def test_has_uncommitted_changes_not_exists(self, mock_run, tmp_path):
        """Test error when worktree doesn't exist."""
        # Setup
        worktree_path = tmp_path / "nonexistent"

        # Execute & Verify
        with pytest.raises(WorktreeError, match="Worktree does not exist"):
            has_uncommitted_changes(worktree_path)

        # Git should not have been called
        mock_run.assert_not_called()

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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
        worktree_path, branch_name = create_worktree(
            repo_root, 123, "Test feature", worktree_base
        )

        assert branch_name == "123-test-feature"
        assert worktree_path == worktree_base / "repo-123"

        # Check for uncommitted changes (should be clean)
        worktree_path.mkdir(parents=True)  # Create for existence check
        result = has_uncommitted_changes(worktree_path)
        assert result is False

        # Remove worktree
        remove_worktree(worktree_path)

    @patch("issue_orchestrator.worktree.subprocess.run")
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
            worktree_path, branch_name = create_worktree(
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
        from issue_orchestrator.worktree import HOOKS_DIR
        
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

    @patch("issue_orchestrator.worktree.subprocess.run")
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

        # Create .githooks directory with project hook (simulating version-controlled hooks)
        custom_hooks_dir = worktree_path / ".githooks"
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
