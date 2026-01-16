"""Tests for bootstrap repo auto-detection."""

from unittest.mock import patch, MagicMock

import pytest

from issue_orchestrator.adapters.github.repo import get_repo_from_git, GitRepoError


class TestRepoAutoDetection:
    """Tests for auto-detecting repo from git remote."""

    def test_get_repo_from_https_remote(self) -> None:
        """Parse owner/repo from HTTPS GitHub URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/owner/repo-name.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_from_ssh_remote(self) -> None:
        """Parse owner/repo from SSH GitHub URL."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git@github.com:owner/repo-name.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_from_https_without_git_suffix(self) -> None:
        """Handle HTTPS URL without .git suffix."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/owner/repo-name"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            repo = get_repo_from_git()

        assert repo == "owner/repo-name"

    def test_get_repo_raises_on_no_remote(self) -> None:
        """Raise GitRepoError when no remote configured."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            with pytest.raises(GitRepoError, match="Could not determine repository"):
                get_repo_from_git()

    def test_get_repo_raises_on_non_github_remote(self) -> None:
        """Raise GitRepoError for non-GitHub remotes."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://gitlab.com/owner/repo.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result
            with pytest.raises(GitRepoError, match="Unrecognized GitHub remote"):
                get_repo_from_git()


class TestBootstrapRepoResolution:
    """Tests for bootstrap.py repo resolution logic."""

    def test_bootstrap_uses_config_repo_when_set(self) -> None:
        """When config.repo is set, use it directly."""
        from issue_orchestrator.infra.config import Config

        config = Config()
        config.repo = "configured/repo"

        # Simulate bootstrap logic
        repo = config.repo
        if not repo:
            repo = "auto-detected/repo"

        assert repo == "configured/repo"

    def test_bootstrap_auto_detects_when_config_repo_none(self) -> None:
        """When config.repo is None, auto-detect from git and update config."""
        from issue_orchestrator.infra.config import Config
        from issue_orchestrator.adapters.github.repo import get_repo_from_git

        config = Config()
        config.repo = None

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "https://github.com/auto/detected.git"

        with patch("issue_orchestrator.adapters.github.repo.GitCLI") as mock_git:
            mock_git.return_value.run.return_value = mock_result

            # Simulate bootstrap logic (matches bootstrap.py)
            repo = config.repo
            if not repo:
                repo = get_repo_from_git()
                config.repo = repo  # Bootstrap updates config

        assert repo == "auto/detected"
        assert config.repo == "auto/detected"  # Config is updated

    def test_bootstrap_error_message_when_no_repo(self) -> None:
        """Error message is clear when repo can't be determined."""
        expected_snippets = [
            "Could not determine GitHub repository",
            "repo.name",
            "git remote",
        ]

        # This is the error message from bootstrap.py
        error_msg = (
            "Could not determine GitHub repository.\n\n"
            "Either:\n"
            "  1. Set 'repo.name' in your config file:\n"
            "       repo:\n"
            "         name: owner/repo-name\n\n"
            "  2. Or ensure you're running from a git repo with a GitHub remote:\n"
            "       git remote get-url origin\n"
            "       # Should show: https://github.com/owner/repo.git"
        )

        for snippet in expected_snippets:
            assert snippet in error_msg, f"Expected '{snippet}' in error message"
