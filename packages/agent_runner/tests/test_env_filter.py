"""Tests for environment filtering."""

import pytest

from agent_runner.env_filter import (
    DEFAULT_FORBIDDEN_ENV_VARS,
    GIT_SAFE_ENV,
    all_env_scrubbed,
    build_filtered_env,
    get_forbidden_env_vars,
    verify_env_scrubbed,
)


class TestBuildFilteredEnv:
    """Tests for build_filtered_env()."""

    def test_scrubs_default_forbidden_vars(self) -> None:
        """Test that default forbidden vars are scrubbed."""
        base_env = {
            "PATH": "/usr/bin",
            "GH_TOKEN": "secret-token",
            "GITHUB_TOKEN": "another-secret",
            "HOME": "/home/user",
        }

        result = build_filtered_env(base_env=base_env)

        assert "PATH" in result
        assert "HOME" in result
        assert "GH_TOKEN" not in result
        assert "GITHUB_TOKEN" not in result

    def test_scrubs_custom_vars(self) -> None:
        """Test that custom scrub vars are removed."""
        base_env = {
            "PATH": "/usr/bin",
            "CUSTOM_SECRET": "secret",
            "KEEP_THIS": "value",
        }

        result = build_filtered_env(
            base_env=base_env,
            scrub_vars=["CUSTOM_SECRET"],
        )

        assert "PATH" in result
        assert "KEEP_THIS" in result
        assert "CUSTOM_SECRET" not in result

    def test_passthrough_allowlist_mode(self) -> None:
        """Test passthrough mode only passes specified vars."""
        base_env = {
            "PATH": "/usr/bin",
            "HOME": "/home/user",
            "ALLOWED_VAR": "value",
            "NOT_ALLOWED": "secret",
        }

        result = build_filtered_env(
            base_env=base_env,
            passthrough_vars=["PATH", "ALLOWED_VAR"],
        )

        assert "PATH" in result
        assert "ALLOWED_VAR" in result
        assert "HOME" not in result
        assert "NOT_ALLOWED" not in result

    def test_overrides_take_precedence(self) -> None:
        """Test that overrides take precedence over base env."""
        base_env = {
            "PATH": "/usr/bin",
            "OVERRIDE_ME": "original",
        }

        result = build_filtered_env(
            base_env=base_env,
            overrides={"OVERRIDE_ME": "new-value", "NEW_VAR": "added"},
        )

        assert result["OVERRIDE_ME"] == "new-value"
        assert result["NEW_VAR"] == "added"
        assert result["PATH"] == "/usr/bin"

    def test_includes_git_safe_env_by_default(self) -> None:
        """Test that git-safe vars are included by default."""
        result = build_filtered_env(base_env={})

        assert result.get("GIT_TERMINAL_PROMPT") == "0"
        assert result.get("GIT_ASKPASS") == "/usr/bin/false"

    def test_excludes_git_safe_env_when_disabled(self) -> None:
        """Test that git-safe vars can be excluded."""
        result = build_filtered_env(base_env={}, include_git_safe=False)

        assert "GIT_TERMINAL_PROMPT" not in result
        assert "GIT_ASKPASS" not in result

    def test_overrides_override_git_safe(self) -> None:
        """Test that overrides can override git-safe vars."""
        result = build_filtered_env(
            base_env={},
            overrides={"GIT_TERMINAL_PROMPT": "1"},
        )

        assert result["GIT_TERMINAL_PROMPT"] == "1"

    def test_scrub_all_credential_vars(self) -> None:
        """Test that all default credential vars are scrubbed."""
        # Create an env with all forbidden vars set
        base_env = {var: f"secret-{var}" for var in DEFAULT_FORBIDDEN_ENV_VARS}
        base_env["SAFE_VAR"] = "keep-me"

        result = build_filtered_env(base_env=base_env)

        for var in DEFAULT_FORBIDDEN_ENV_VARS:
            assert var not in result, f"{var} should be scrubbed"
        assert result["SAFE_VAR"] == "keep-me"


class TestVerifyEnvScrubbed:
    """Tests for verify_env_scrubbed()."""

    def test_all_absent_returns_true(self) -> None:
        """Test that absent vars return True."""
        env = {"PATH": "/usr/bin"}

        result = verify_env_scrubbed(env, forbidden=["GH_TOKEN", "GITHUB_TOKEN"])

        assert result == {"GH_TOKEN": True, "GITHUB_TOKEN": True}

    def test_present_vars_return_false(self) -> None:
        """Test that present vars return False."""
        env = {"GH_TOKEN": "secret", "PATH": "/usr/bin"}

        result = verify_env_scrubbed(env, forbidden=["GH_TOKEN", "GITHUB_TOKEN"])

        assert result == {"GH_TOKEN": False, "GITHUB_TOKEN": True}

    def test_uses_default_forbidden_list(self) -> None:
        """Test that default forbidden list is used when not specified."""
        env = {}

        result = verify_env_scrubbed(env)

        assert len(result) == len(DEFAULT_FORBIDDEN_ENV_VARS)
        assert all(result.values())


class TestAllEnvScrubbed:
    """Tests for all_env_scrubbed()."""

    def test_all_scrubbed_returns_true(self) -> None:
        """Test that fully scrubbed env returns True."""
        env = {"PATH": "/usr/bin", "HOME": "/home/user"}

        assert all_env_scrubbed(env, forbidden=["GH_TOKEN", "SSH_AUTH_SOCK"])

    def test_any_present_returns_false(self) -> None:
        """Test that any present forbidden var returns False."""
        env = {"PATH": "/usr/bin", "GH_TOKEN": "secret"}

        assert not all_env_scrubbed(env, forbidden=["GH_TOKEN", "SSH_AUTH_SOCK"])


class TestGetForbiddenEnvVars:
    """Tests for get_forbidden_env_vars()."""

    def test_returns_copy(self) -> None:
        """Test that a copy is returned, not the original list."""
        result = get_forbidden_env_vars()

        # Modify the result
        result.append("CUSTOM_VAR")

        # Original should be unchanged
        assert "CUSTOM_VAR" not in DEFAULT_FORBIDDEN_ENV_VARS

    def test_contains_expected_vars(self) -> None:
        """Test that expected credential vars are in the list."""
        result = get_forbidden_env_vars()

        assert "GH_TOKEN" in result
        assert "GITHUB_TOKEN" in result
        assert "SSH_AUTH_SOCK" in result
        assert "AWS_SECRET_ACCESS_KEY" in result
