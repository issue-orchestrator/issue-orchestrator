"""Unit tests for the sandbox verification module."""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch
from issue_orchestrator.ports.git import GitError, GitResult

from issue_orchestrator.execution.sandbox_verify import (
    verify_git_push_fails,
    verify_env_vars_absent,
    verify_home_isolated,
    verify_sandbox,
)

class TestVerifyGitPushFails:
    """Tests for git push verification."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_passes_when_push_fails(self, temp_worktree):
        """Test passes when git push fails."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            mock_run.return_value = GitResult(argv=["git"], returncode=128, stdout="", stderr="")
            result = verify_git_push_fails(temp_worktree)
            assert result.passed is True
            assert "fails" in result.message.lower()

    def test_fails_when_push_succeeds(self, temp_worktree):
        """Test fails when git push would succeed."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            mock_run.return_value = GitResult(argv=["git"], returncode=0, stdout="", stderr="")
            result = verify_git_push_fails(temp_worktree)
            assert result.passed is False
            assert "succeeded" in result.message.lower()

    def test_warning_on_timeout(self, temp_worktree):
        """Test returns warning on timeout."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            mock_run.side_effect = GitError(
                GitResult(argv=["git"], returncode=-1, stdout="", stderr=""),
                message="git command timed out",
            )
            result = verify_git_push_fails(temp_worktree)
            assert result.passed is False
            assert result.critical is False  # Warning, not critical

class TestVerifyEnvVarsAbsent:
    """Tests for environment variable verification."""

    def test_passes_when_no_forbidden_vars(self):
        """Test passes when no forbidden vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            result = verify_env_vars_absent()
            assert result.passed is True
            assert "absent" in result.message.lower()

    def test_fails_when_forbidden_vars_present(self):
        """Test fails when forbidden vars are set."""
        with patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True):
            result = verify_env_vars_absent()
            assert result.passed is False
            assert "GH_TOKEN" in result.message

class TestVerifyHomeIsolated:
    """Tests for HOME isolation verification."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_passes_when_home_is_worktree(self, temp_worktree):
        """Test passes when HOME matches worktree."""
        with patch.dict(os.environ, {"HOME": str(temp_worktree)}):
            result = verify_home_isolated(temp_worktree)
            assert result.passed is True

    def test_fails_when_home_differs(self, temp_worktree):
        """Test fails when HOME differs from worktree."""
        with patch.dict(os.environ, {"HOME": "/some/other/path"}):
            result = verify_home_isolated(temp_worktree)
            assert result.passed is False
            assert result.critical is False  # This is a warning

class TestVerifySandbox:
    """Tests for full sandbox verification."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_all_pass_returns_all_passed(self, temp_worktree):
        """Test all_passed is True when all checks pass."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            # git push fails (good)
            mock_run.return_value = GitResult(argv=["git"], returncode=1, stdout="", stderr="")
            with patch.dict(os.environ, {"HOME": str(temp_worktree)}, clear=True):
                result = verify_sandbox(worktree=temp_worktree)
                assert result.all_passed is True
                assert len(result.critical_failures) == 0

    def test_critical_failure_returns_not_passed(self, temp_worktree):
        """Test all_passed is False when critical check fails."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            # git push succeeds (bad)
            mock_run.return_value = GitResult(argv=["git"], returncode=0, stdout="", stderr="")
            with patch.dict(os.environ, {}, clear=True):
                result = verify_sandbox(worktree=temp_worktree)
                assert result.all_passed is False
                assert "git_push_fails" in result.critical_failures

    def test_warning_does_not_fail(self, temp_worktree):
        """Test warnings don't cause all_passed to be False."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            # Critical checks pass
            mock_run.return_value = GitResult(argv=["git"], returncode=1, stdout="", stderr="")
            # But HOME is not isolated (warning)
            with patch.dict(os.environ, {"HOME": "/other/path"}, clear=True):
                result = verify_sandbox(
                    worktree=temp_worktree,
                    check_git_push=False,  # Skip to simplify
                )
                # all_passed should be True because HOME is just a warning
                assert result.all_passed is True
                assert "home_isolated" in result.warnings

    def test_can_skip_checks(self, temp_worktree):
        """Test individual checks can be skipped."""
        with patch.dict(os.environ, {}, clear=True):
            result = verify_sandbox(
                worktree=temp_worktree,
                check_git_push=False,
                check_env_vars=True,
                check_home=False,
            )
            # Only env_vars check should have run
            assert len(result.results) == 1
            assert result.results[0].name == "env_vars_absent"

    def test_summary_includes_failures(self, temp_worktree):
        """Test summary lists failed checks."""
        with patch("issue_orchestrator.execution.sandbox_verify.GitCLI.run") as mock_run:
            mock_run.return_value = GitResult(argv=["git"], returncode=0, stdout="", stderr="")
            with patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True):
                result = verify_sandbox(
                    worktree=temp_worktree,
                    check_git_push=False,
                    check_home=False,
                )
                assert "failed" in result.summary.lower()
                assert "gh_auth_unavailable" in result.summary or "env_vars_absent" in result.summary
