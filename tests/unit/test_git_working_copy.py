"""Unit tests for GitWorkingCopy adapter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.ports.git import GitError, GitResult
from issue_orchestrator.ports.working_copy import (
    BranchStatus,
    CommitInfo,
    PreflightResult,
    PushResult,
    RebaseResult,
)


def git_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> GitResult:
    return GitResult(argv=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


def git_error(*_args, **kwargs) -> GitError:
    stdout = kwargs.get("stdout", "")
    stderr = kwargs.get("stderr", "")
    return GitError(git_result(stdout=stdout, stderr=stderr, returncode=1))


@pytest.fixture
def git_wc():
    """Create a GitWorkingCopy instance."""
    return GitWorkingCopy()


@pytest.fixture
def worktree_path(tmp_path):
    """Create a temporary worktree path."""
    return tmp_path / "worktree"


class TestRunGit:
    """Tests for the _run_git internal helper."""

    def test_run_git_success(self, git_wc, worktree_path):
        """Test successful git command execution."""
        with patch.object(git_wc._git, "run") as mock_run:
            mock_run.return_value = git_result(stdout="output")

            result = git_wc._run_git(worktree_path, ["status"])

            mock_run.assert_called_once_with(
                worktree_path,
                ["status"],
                check=True,
                timeout_s=None,
            )
            assert result.stdout == "output"

    def test_run_git_with_check_false(self, git_wc, worktree_path):
        """Test git command with check=False."""
        with patch.object(git_wc._git, "run") as mock_run:
            mock_run.return_value = git_result(returncode=1)

            git_wc._run_git(worktree_path, ["status"], check=False)

            mock_run.assert_called_once_with(
                worktree_path,
                ["status"],
                check=False,
                timeout_s=None,
            )

    def test_run_git_with_timeout(self, git_wc, worktree_path):
        """Test git command with a timeout override."""
        with patch.object(git_wc._git, "run") as mock_run:
            mock_run.return_value = git_result()

            git_wc._run_git(worktree_path, ["status"], timeout_s=5)

            mock_run.assert_called_once_with(
                worktree_path,
                ["status"],
                check=True,
                timeout_s=5,
            )


class TestGetCurrentBranch:
    """Tests for get_current_branch method."""

    def test_get_current_branch_success(self, git_wc, worktree_path):
        """Test getting current branch successfully."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="feature-branch\n",
                stderr="",
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch == "feature-branch"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--abbrev-ref" in args
            assert "HEAD" in args

    def test_get_current_branch_detached_head(self, git_wc, worktree_path):
        """Test getting current branch when in detached HEAD state."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="HEAD\n",
                stderr="",
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch is None

    def test_get_current_branch_error(self, git_wc, worktree_path):
        """Test getting current branch when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="not a git repository"
            )

            branch = git_wc.get_current_branch(worktree_path)

            assert branch is None


class TestGetHeadSha:
    """Tests for get_head_sha method."""

    def test_get_head_sha_success(self, git_wc, worktree_path):
        """Test getting HEAD SHA successfully."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="abc123def456\n",
                stderr="",
            )

            sha = git_wc.get_head_sha(worktree_path)

            assert sha == "abc123def456"
            mock_run.assert_called_once()
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "HEAD" in args

    def test_get_head_sha_error(self, git_wc, worktree_path):
        """Test getting HEAD SHA when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="not a git repository"
            )

            sha = git_wc.get_head_sha(worktree_path)

            assert sha is None


class TestGetBranchStatus:
    """Tests for get_branch_status method."""

    def test_get_branch_status_clean_with_upstream(self, git_wc, worktree_path):
        """Test getting branch status with clean state and upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # First call: get current branch
            # Second call: git status --porcelain (clean)
            # Third call: git rev-list for ahead/behind counts
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),  # clean
                MagicMock(returncode=0, stdout="3\t2\n", stderr=""),  # 3 ahead, 2 behind
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is not None
            assert status.branch == "main"
            assert status.clean is True
            assert status.has_remote is True
            assert status.ahead == 3
            assert status.behind == 2

    def test_get_branch_status_dirty_no_upstream(self, git_wc, worktree_path):
        """Test getting branch status with uncommitted changes and no upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout=" M file.txt\n", stderr=""),  # dirty
                git_error(1, "git", stderr="no upstream"),
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is not None
            assert status.branch == "feature-branch"
            assert status.clean is False
            assert status.has_remote is False
            assert status.ahead == 0
            assert status.behind == 0

    def test_get_branch_status_detached_head(self, git_wc, worktree_path):
        """Test getting branch status when in detached HEAD state."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            status = git_wc.get_branch_status(worktree_path)

            assert status is None

    def test_get_branch_status_error(self, git_wc, worktree_path):
        """Test getting branch status when git command fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="main\n", stderr=""),
                git_error(1, "git", stderr="error"),
            ]

            status = git_wc.get_branch_status(worktree_path)

            assert status is None


class TestHasUncommittedChanges:
    """Tests for has_uncommitted_changes method."""

    def test_has_uncommitted_changes_true(self, git_wc, worktree_path):
        """Test detecting uncommitted changes."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=" M file.txt\nA  newfile.py\n",
                stderr="",
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is True

    def test_has_uncommitted_changes_false(self, git_wc, worktree_path):
        """Test clean working directory."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is False

    def test_has_uncommitted_changes_error(self, git_wc, worktree_path):
        """Test error handling - assume dirty on error for safety."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            has_changes = git_wc.has_uncommitted_changes(worktree_path)

            assert has_changes is True  # Safer to assume dirty


class TestGetCommitsAheadOfMain:
    """Tests for get_commits_ahead_of_main method."""

    def test_get_commits_ahead_of_main_success(self, git_wc, worktree_path):
        """Test getting commits ahead of main."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "abc123|Add feature X|Alice|abc12\n"
                    "def456|Fix bug Y|Bob|def45\n"
                ),
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert len(commits) == 2
            assert commits[0].sha == "abc123"
            assert commits[0].message == "Add feature X"
            assert commits[0].author == "Alice"
            assert commits[0].short_sha == "abc12"
            assert commits[1].sha == "def456"
            assert commits[1].message == "Fix bug Y"
            assert commits[1].author == "Bob"
            assert commits[1].short_sha == "def45"

    def test_get_commits_ahead_of_main_empty(self, git_wc, worktree_path):
        """Test when no commits ahead of main."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert commits == []

    def test_get_commits_ahead_of_main_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            assert commits == []

    def test_get_commits_ahead_of_main_malformed(self, git_wc, worktree_path):
        """Test handling malformed output."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=(
                    "abc123|Add feature X|Alice|abc12\n"
                    "malformed line\n"
                    "def456|Fix bug Y|Bob|def45\n"
                ),
                stderr="",
            )

            commits = git_wc.get_commits_ahead_of_main(worktree_path)

            # Should skip malformed line
            assert len(commits) == 2


class TestFetch:
    """Tests for fetch method."""

    def test_fetch_success(self, git_wc, worktree_path):
        """Test successful fetch."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.fetch(worktree_path)

            assert result is True
            args = mock_run.call_args[0][1]
            assert "fetch" in args
            assert "origin" in args

    def test_fetch_custom_remote(self, git_wc, worktree_path):
        """Test fetch with custom remote."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.fetch(worktree_path, remote="upstream")

            assert result is True
            args = mock_run.call_args[0][1]
            assert "fetch" in args
            assert "upstream" in args

    def test_fetch_error(self, git_wc, worktree_path):
        """Test fetch failure."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="network error"
            )

            result = git_wc.fetch(worktree_path)

            assert result is False


class TestListRemoteBranches:
    """Tests for list_remote_branches method."""

    def test_list_remote_branches_success(self, git_wc, worktree_path):
        """Test listing remote branches."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="  origin/main\n  origin/feature-1\n  origin/feature-2\n",
                stderr="",
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert len(branches) == 3
            assert "origin/main" in branches
            assert "origin/feature-1" in branches
            assert "origin/feature-2" in branches

    def test_list_remote_branches_empty(self, git_wc, worktree_path):
        """Test listing remote branches when none exist."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert branches == []

    def test_list_remote_branches_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            branches = git_wc.list_remote_branches(worktree_path)

            assert branches == []


class TestIsGitRepo:
    """Tests for is_git_repo method."""

    def test_is_git_repo_true(self, git_wc, worktree_path):
        """Test detecting a git repository."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=".git\n",
                stderr="",
            )

            result = git_wc.is_git_repo(worktree_path)

            assert result is True
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--git-dir" in args

    def test_is_git_repo_false(self, git_wc, worktree_path):
        """Test detecting non-git directory."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                128, "git", stderr="not a git repository"
            )

            result = git_wc.is_git_repo(worktree_path)

            assert result is False


class TestGetConfigValue:
    """Tests for get_config_value method."""

    def test_get_config_value_success(self, git_wc, worktree_path):
        """Test getting config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="user@example.com\n",
                stderr="",
            )

            value = git_wc.get_config_value(worktree_path, "user.email")

            assert value == "user@example.com"
            args = mock_run.call_args[0][1]
            assert "config" in args
            assert "--get" in args
            assert "user.email" in args

    def test_get_config_value_empty(self, git_wc, worktree_path):
        """Test getting empty config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            value = git_wc.get_config_value(worktree_path, "some.key")

            assert value is None

    def test_get_config_value_not_found(self, git_wc, worktree_path):
        """Test getting non-existent config value."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="key not found"
            )

            value = git_wc.get_config_value(worktree_path, "nonexistent.key")

            assert value is None


class TestGetCommitsAheadCount:
    """Tests for get_commits_ahead_count method."""

    def test_get_commits_ahead_count_success(self, git_wc, worktree_path):
        """Test counting commits ahead."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="5\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 5
            args = mock_run.call_args[0][1]
            assert "rev-list" in args
            assert "--count" in args
            assert "origin/main..origin/feature-branch" in args

    def test_get_commits_ahead_count_zero(self, git_wc, worktree_path):
        """Test when no commits ahead."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="0\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0

    def test_get_commits_ahead_count_custom_base(self, git_wc, worktree_path):
        """Test with custom base branch."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="3\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(
                worktree_path, "feature-branch", base="origin/develop"
            )

            assert count == 3
            args = mock_run.call_args[0][1]
            assert "origin/develop..origin/feature-branch" in args

    def test_get_commits_ahead_count_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0

    def test_get_commits_ahead_count_invalid_output(self, git_wc, worktree_path):
        """Test handling invalid output."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="not a number\n",
                stderr="",
            )

            count = git_wc.get_commits_ahead_count(worktree_path, "feature-branch")

            assert count == 0


class TestGetLastCommitDate:
    """Tests for get_last_commit_date method."""

    def test_get_last_commit_date_success(self, git_wc, worktree_path):
        """Test getting last commit date."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="2 hours ago\n",
                stderr="",
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date == "2 hours ago"
            args = mock_run.call_args[0][1]
            assert "log" in args
            assert "-1" in args
            assert "--format=%cr" in args
            assert "origin/feature-branch" in args

    def test_get_last_commit_date_empty(self, git_wc, worktree_path):
        """Test when output is empty."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="",
                stderr="",
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date is None

    def test_get_last_commit_date_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error"
            )

            date = git_wc.get_last_commit_date(worktree_path, "feature-branch")

            assert date is None


class TestRebaseOnBranch:
    """Tests for rebase_on_branch method."""

    def test_rebase_success(self, git_wc, worktree_path):
        """Test successful rebase."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.rebase_on_branch(worktree_path)

            assert result.success is True
            assert "origin/main" in result.message
            assert result.conflicts is None
            assert result.aborted is False

    def test_rebase_custom_target(self, git_wc, worktree_path):
        """Test rebase with custom target."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            result = git_wc.rebase_on_branch(worktree_path, target="origin/develop")

            assert result.success is True
            assert "origin/develop" in result.message
            args = mock_run.call_args[0][1]
            assert "rebase" in args
            assert "origin/develop" in args

    def test_rebase_with_conflicts(self, git_wc, worktree_path):
        """Test rebase failure with conflicts."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # First call: rebase fails
            # Second call: git status shows conflicts
            # Third call: rebase --abort succeeds
            mock_run.side_effect = [
                git_error(
                    1, "git", stderr="CONFLICT (content): Merge conflict"
                ),
                MagicMock(
                    returncode=0,
                    stdout="UU conflicted_file.py\nUU another_conflict.txt\n",
                    stderr="",
                ),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.rebase_on_branch(worktree_path)

            assert result.success is False
            assert "conflicts" in result.message.lower()
            assert result.conflicts == ["conflicted_file.py", "another_conflict.txt"]
            assert result.aborted is True

    def test_rebase_abort_fails(self, git_wc, worktree_path):
        """Test rebase failure where abort also fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                git_error(1, "git", stderr="CONFLICT"),
                MagicMock(returncode=0, stdout="UU file.py\n", stderr=""),
                git_error(1, "git", stderr="abort failed"),
            ]

            result = git_wc.rebase_on_branch(worktree_path)

            assert result.success is False
            assert result.aborted is False

    def test_rebase_error_no_conflicts(self, git_wc, worktree_path):
        """Test rebase failure without conflicts."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                git_error(1, "git", stderr="fatal: some error"),
                MagicMock(returncode=0, stdout="", stderr=""),  # No conflicts
            ]

            result = git_wc.rebase_on_branch(worktree_path)

            assert result.success is False
            assert result.conflicts is None or result.conflicts == []


class TestPush:
    """Tests for push method."""

    def test_push_success_with_existing_remote(self, git_wc, worktree_path):
        """Test successful push when remote branch exists (subsequent push)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, rev-parse (exists), fetch, push
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path)

            assert result.success is True
            assert result.branch == "feature-branch"
            assert result.remote == "origin"
            assert "Pushed" in result.message

            # Check rev-parse for tracking ref
            revparse_args = mock_run.call_args_list[1][0][1]
            assert "rev-parse" in revparse_args
            assert "origin/feature-branch" in revparse_args

            # Check fetch command
            fetch_args = mock_run.call_args_list[2][0][1]
            assert "fetch" in fetch_args

            # Check push command
            push_args = mock_run.call_args_list[3][0][1]
            assert "push" in push_args
            assert "--force-with-lease" in push_args

    def test_push_first_push_no_remote(self, git_wc, worktree_path):
        """Test first push when remote branch doesn't exist yet."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Flow: get_branch, rev-parse (not found), push (no fetch)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=1, stdout="", stderr=""),  # tracking doesn't exist
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path)

            assert result.success is True
            # Only 3 calls - no fetch needed
            assert mock_run.call_count == 3
            push_args = mock_run.call_args_list[2][0][1]
            assert "push" in push_args

    def test_push_fetch_failure(self, git_wc, worktree_path):
        """Test push fails early when fetch fails (with tracking ref)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                Exception("Network timeout"),  # fetch fails
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Failed to update tracking refs" in result.message
            assert result.retryable is True

    def test_push_no_branch(self, git_wc, worktree_path):
        """Test push when current branch cannot be determined."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.branch == ""
            assert "Could not determine current branch" in result.message
            assert result.retryable is False

    def test_push_with_skip_hooks(self, git_wc, worktree_path):
        """Test push with skip_hooks enabled."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, skip_hooks=True)

            assert result.success is True
            push_args = mock_run.call_args_list[3][0][1]
            assert "--no-verify" in push_args

    def test_push_without_set_upstream(self, git_wc, worktree_path):
        """Test push without setting upstream."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, set_upstream=False)

            assert result.success is True
            push_args = mock_run.call_args_list[3][0][1]
            assert "-u" not in push_args

    def test_push_non_fast_forward(self, git_wc, worktree_path):
        """Test push failure due to non-fast-forward."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="error: failed to push some refs (non-fast-forward)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.branch == "feature-branch"
            assert "non-fast-forward" in result.message
            assert result.retryable is False  # Needs force or rebase

    def test_push_rejected(self, git_wc, worktree_path):
        """Test push failure due to rejected refs."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="error: failed to push (rejected)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "rejected" in result.message
            assert result.retryable is False

    def test_push_permission_denied(self, git_wc, worktree_path):
        """Test push failure due to permission denied."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="Permission denied (publickey)"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Permission denied" in result.message
            assert result.retryable is False  # Auth issue

    def test_push_network_error(self, git_wc, worktree_path):
        """Test push failure due to network error (retryable)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                git_error(
                    1, "git", stderr="Could not resolve host: github.com"
                ),
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert result.retryable is True  # Network issues are retryable

    def test_push_custom_remote(self, git_wc, worktree_path):
        """Test push to custom remote."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                MagicMock(returncode=0, stdout="", stderr=""),  # push
            ]

            result = git_wc.push(worktree_path, remote="upstream")

            assert result.success is True
            assert result.remote == "upstream"
            # Check rev-parse uses custom remote
            revparse_args = mock_run.call_args_list[1][0][1]
            assert "upstream/feature-branch" in revparse_args
            # Check push uses custom remote
            push_args = mock_run.call_args_list[3][0][1]
            assert "upstream" in push_args


class TestGetIssueNumberFromBranch:
    """Tests for get_issue_number_from_branch method."""

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_canonical_format(
        self, mock_extract, git_wc, worktree_path
    ):
        """Test extracting issue number from canonical format."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="328-add-feature\n", stderr=""
            )
            mock_extract.return_value = 328

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 328
            mock_extract.assert_called_once_with("328-add-feature")

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_legacy_format(
        self, mock_extract, git_wc, worktree_path
    ):
        """Test fallback to legacy format (issue-123)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="issue-123\n", stderr=""
            )
            mock_extract.return_value = None  # Canonical format doesn't match

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 123

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_slash_format(self, mock_extract, git_wc, worktree_path):
        """Test fallback to slash format (feature/456-thing)."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="feature/456-thing\n", stderr=""
            )
            mock_extract.return_value = None

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num == 456

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_no_match(self, mock_extract, git_wc, worktree_path):
        """Test when branch name doesn't contain issue number."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="main\n", stderr=""
            )
            mock_extract.return_value = None

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num is None

    @patch("issue_orchestrator.adapters.worktree._worktree.extract_issue_number_from_branch")
    def test_get_issue_number_no_branch(self, mock_extract, git_wc, worktree_path):
        """Test when current branch cannot be determined."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="HEAD\n", stderr="")

            issue_num = git_wc.get_issue_number_from_branch(worktree_path)

            assert issue_num is None
            mock_extract.assert_not_called()


class TestGetWorktreeRoot:
    """Tests for get_worktree_root method."""

    def test_get_worktree_root_success(self, git_wc, worktree_path):
        """Test getting worktree root."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="/path/to/repo\n",
                stderr="",
            )

            root = git_wc.get_worktree_root(worktree_path)

            assert root == Path("/path/to/repo")
            args = mock_run.call_args[0][1]
            assert "rev-parse" in args
            assert "--show-toplevel" in args

    def test_get_worktree_root_error(self, git_wc, worktree_path):
        """Test error handling."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                128, "git", stderr="not a git repository"
            )

            root = git_wc.get_worktree_root(worktree_path)

            assert root is None


class TestCommitAll:
    """Tests for commit_all method."""

    def test_commit_all_success(self, git_wc, worktree_path):
        """Test successful commit."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # First call: git add -A
            # Second call: git commit
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is True

            # Check git add command
            add_args = mock_run.call_args_list[0][0][1]
            assert "add" in add_args
            assert "-A" in add_args

            # Check git commit command
            commit_args = mock_run.call_args_list[1][0][1]
            assert "commit" in commit_args
            assert "-m" in commit_args
            assert "Test commit" in commit_args

    def test_commit_all_with_allow_empty(self, git_wc, worktree_path):
        """Test commit with allow_empty flag."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]

            result = git_wc.commit_all(worktree_path, "Empty commit", allow_empty=True)

            assert result is True
            commit_args = mock_run.call_args_list[1][0][1]
            assert "--allow-empty" in commit_args

    def test_commit_all_nothing_to_commit(self, git_wc, worktree_path):
        """Test when there's nothing to commit."""
        with patch.object(git_wc, "_run_git") as mock_run:
            # Create an error with stdout set on the result
            error = git_error(stdout="nothing to commit, working tree clean", stderr="")

            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                error,
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            # Should return True for "nothing to commit" - not an error
            assert result is True

    def test_commit_all_add_fails(self, git_wc, worktree_path):
        """Test when git add fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = git_error(
                1, "git", stderr="error adding files"
            )

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is False

    def test_commit_all_commit_fails(self, git_wc, worktree_path):
        """Test when git commit fails."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="", stderr=""),
                git_error(
                    1, "git", stderr="Author identity unknown"
                ),
            ]

            result = git_wc.commit_all(worktree_path, "Test commit")

            assert result is False


class TestPushPreflight:
    """Tests for push_preflight method."""

    def test_push_preflight_success_with_tracking(self, git_wc, worktree_path):
        """Test successful preflight when remote tracking branch exists."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: rev-parse (exists), fetch, push --dry-run
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    MagicMock(returncode=0, stdout="", stderr=""),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert result.error is None
                assert mock_run.call_count == 3

    def test_push_preflight_success_first_push(self, git_wc, worktree_path):
        """Test successful preflight when no remote tracking (first push)."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                # Flow: rev-parse (not found), push --dry-run (no fetch)
                mock_run.side_effect = [
                    MagicMock(returncode=1, stdout="", stderr=""),  # no tracking
                    MagicMock(returncode=0, stdout="", stderr=""),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is True
                assert mock_run.call_count == 2  # No fetch

    def test_push_preflight_fetch_failure(self, git_wc, worktree_path):
        """Test preflight fails early when fetch fails."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    Exception("Network timeout"),  # fetch fails
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "Failed to update tracking refs" in result.error
                assert result.fix_hint is not None

    def test_push_preflight_no_branch(self, git_wc, worktree_path):
        """Test when current branch cannot be determined."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = None

            result = git_wc.push_preflight(worktree_path)

            assert result.would_succeed is False
            assert "branch" in result.error.lower()
            assert result.fix_hint is not None

    def test_push_preflight_stale_info_error(self, git_wc, worktree_path):
        """Test handling stale info error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="stale info detected"),  # push fails
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "stale info" in result.error
                assert result.fix_hint is not None
                assert "rebase" in result.fix_hint.lower()

    def test_push_preflight_rejected_error(self, git_wc, worktree_path):
        """Test handling rejected push error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="! [rejected] non-fast-forward"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "rejected" in result.error
                assert result.fix_hint is not None

    def test_push_preflight_permission_denied(self, git_wc, worktree_path):
        """Test handling permission denied error."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    git_error(stderr="Permission denied (publickey)"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "permission denied" in result.error.lower()
                assert "authentication" in result.fix_hint.lower()

    def test_push_preflight_timeout(self, git_wc, worktree_path):
        """Test handling timeout error on push."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    Exception("command timed out"),  # push
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "timed out" in result.error.lower()
                assert result.fix_hint is not None

    def test_push_preflight_custom_remote(self, git_wc, worktree_path):
        """Test with custom remote."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    MagicMock(returncode=0, stdout="", stderr=""),  # fetch
                    MagicMock(returncode=0, stdout="", stderr=""),  # push
                ]

                result = git_wc.push_preflight(worktree_path, remote="upstream")

                assert result.would_succeed is True
                # Check rev-parse uses custom remote
                revparse_args = mock_run.call_args_list[0][0][1]
                assert "upstream/feature-branch" in revparse_args


class TestPushFetchFailureStopsProcessing:
    """Test that fetch failures stop processing early."""

    def test_push_fetch_failure_prevents_push(self, git_wc, worktree_path):
        """Verify that when fetch fails, push is never attempted."""
        with patch.object(git_wc, "_run_git") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="feature-branch\n", stderr=""),  # get_branch
                MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                Exception("Network timeout"),  # fetch fails
            ]

            result = git_wc.push(worktree_path)

            assert result.success is False
            assert "Failed to update tracking refs" in result.message
            # Only 3 calls - push was never attempted
            assert mock_run.call_count == 3

    def test_preflight_fetch_failure_prevents_dry_run(self, git_wc, worktree_path):
        """Verify that when fetch fails in preflight, dry-run is never attempted."""
        with patch.object(git_wc, "get_current_branch") as mock_branch:
            mock_branch.return_value = "feature-branch"

            with patch.object(git_wc, "_run_git") as mock_run:
                mock_run.side_effect = [
                    MagicMock(returncode=0, stdout="abc123\n", stderr=""),  # tracking exists
                    Exception("Connection refused"),  # fetch fails
                ]

                result = git_wc.push_preflight(worktree_path)

                assert result.would_succeed is False
                assert "Failed to update tracking refs" in result.error
                # Only 2 calls - dry-run push was never attempted
                assert mock_run.call_count == 2
