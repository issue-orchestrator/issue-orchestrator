"""Unit tests for the isolation module."""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.control.isolation import (
    FORBIDDEN_ENV_VARS,
    get_forbidden_env_vars,
    build_env_unset_commands,
    build_home_isolation_command,
    build_isolation_prefix,
    verify_env_scrubbed,
    all_env_scrubbed,
)


class TestForbiddenEnvVars:
    """Tests for forbidden environment variable list."""

    def test_forbidden_vars_includes_github_tokens(self):
        """Test that common GitHub tokens are in the forbidden list."""
        assert "GH_TOKEN" in FORBIDDEN_ENV_VARS
        assert "GITHUB_TOKEN" in FORBIDDEN_ENV_VARS

    def test_forbidden_vars_includes_aws_credentials(self):
        """Test that AWS credentials are in the forbidden list."""
        assert "AWS_ACCESS_KEY_ID" in FORBIDDEN_ENV_VARS
        assert "AWS_SECRET_ACCESS_KEY" in FORBIDDEN_ENV_VARS

    def test_get_forbidden_env_vars_returns_copy(self):
        """Test that get_forbidden_env_vars returns a copy."""
        vars1 = get_forbidden_env_vars()
        vars2 = get_forbidden_env_vars()
        vars1.append("TEST_VAR")
        assert "TEST_VAR" not in vars2


class TestBuildEnvUnsetCommands:
    """Tests for building unset commands."""

    def test_builds_unset_for_each_var(self):
        """Test that unset command is built for each forbidden var."""
        commands = build_env_unset_commands()
        assert len(commands) == len(FORBIDDEN_ENV_VARS)
        assert all(cmd.startswith("unset ") for cmd in commands)

    def test_includes_gh_token(self):
        """Test that GH_TOKEN unset is included."""
        commands = build_env_unset_commands()
        assert "unset GH_TOKEN" in commands


class TestBuildHomeIsolationCommand:
    """Tests for HOME isolation command building."""

    def test_builds_export_command(self):
        """Test that export command is built correctly."""
        worktree = Path("/path/to/worktree")
        cmd = build_home_isolation_command(worktree)
        assert cmd == 'export HOME="/path/to/worktree"'

    def test_handles_spaces_in_path(self):
        """Test that paths with spaces are quoted."""
        worktree = Path("/path/with spaces/worktree")
        cmd = build_home_isolation_command(worktree)
        assert cmd == 'export HOME="/path/with spaces/worktree"'


class TestBuildIsolationPrefix:
    """Tests for building the full isolation prefix."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_standard_mode_includes_home_isolation(self, temp_worktree):
        """Test that standard mode includes HOME isolation."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=False,
            isolate_home=True,
        )
        assert f'export HOME="{temp_worktree}"' in prefix
        assert prefix.endswith(" && ")

    def test_non_standard_mode_skips_home_isolation(self, temp_worktree):
        """Test that non-standard mode skips HOME isolation."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="hardened",
            scrub_env=False,
            isolate_home=True,
        )
        assert "HOME" not in prefix

    def test_env_scrubbing_included(self, temp_worktree):
        """Test that env scrubbing commands are included."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,
        )
        assert "unset GH_TOKEN" in prefix
        assert "unset GITHUB_TOKEN" in prefix

    def test_full_prefix_combines_all(self, temp_worktree):
        """Test that full prefix combines scrubbing and HOME isolation."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=True,
        )
        assert "unset GH_TOKEN" in prefix
        assert f'export HOME="{temp_worktree}"' in prefix
        # Should be joined with &&
        assert " && " in prefix
        # Should end with && for command chaining
        assert prefix.endswith(" && ")

    def test_empty_when_disabled(self, temp_worktree):
        """Test that empty string returned when all disabled."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=False,
            isolate_home=False,
        )
        assert prefix == ""


class TestVerifyEnvScrubbed:
    """Tests for verifying environment scrubbing."""

    def test_reports_absent_vars_as_true(self):
        """Test that absent variables are reported as True."""
        # Ensure GH_TOKEN is not set
        with patch.dict(os.environ, {}, clear=True):
            results = verify_env_scrubbed()
            assert results["GH_TOKEN"] is True
            assert results["GITHUB_TOKEN"] is True

    def test_reports_present_vars_as_false(self):
        """Test that present variables are reported as False."""
        with patch.dict(os.environ, {"GH_TOKEN": "secret123"}, clear=True):
            results = verify_env_scrubbed()
            assert results["GH_TOKEN"] is False
            assert results["GITHUB_TOKEN"] is True  # Not set


class TestAllEnvScrubbed:
    """Tests for all_env_scrubbed helper."""

    def test_returns_true_when_all_absent(self):
        """Test returns True when no forbidden vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            assert all_env_scrubbed() is True

    def test_returns_false_when_any_present(self):
        """Test returns False when any forbidden var is set."""
        with patch.dict(os.environ, {"GH_TOKEN": "secret"}, clear=True):
            assert all_env_scrubbed() is False
