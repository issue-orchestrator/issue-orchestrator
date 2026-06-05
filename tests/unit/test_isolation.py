"""Unit tests for the isolation module."""

import os
import pytest
import shlex
import tempfile
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.control.isolation import (
    FORBIDDEN_ENV_VARS,
    GIT_SAFE_ENV,
    GRADLE_USER_HOME_ENV,
    get_forbidden_env_vars,
    get_gradle_user_home,
    get_orchestrator_socket_path,
    build_runtime_tool_env,
    build_runtime_tool_env_assignments,
    build_env_unset_commands,
    build_git_safe_commands,
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

    def test_forbidden_vars_includes_ssh_auth_sock(self):
        """Test that SSH_AUTH_SOCK is in the forbidden list."""
        assert "SSH_AUTH_SOCK" in FORBIDDEN_ENV_VARS

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

    def test_includes_ssh_auth_sock(self):
        """Test that SSH_AUTH_SOCK unset is included."""
        commands = build_env_unset_commands()
        assert "unset SSH_AUTH_SOCK" in commands


class TestGitSafeEnv:
    """Tests for git-safe environment variables."""

    def test_git_terminal_prompt_set(self):
        """Test that GIT_TERMINAL_PROMPT is set to 0."""
        assert GIT_SAFE_ENV["GIT_TERMINAL_PROMPT"] == "0"

    def test_git_askpass_set(self):
        """Test that GIT_ASKPASS is set to /usr/bin/false."""
        assert GIT_SAFE_ENV["GIT_ASKPASS"] == "/usr/bin/false"


class TestRuntimeToolEnv:
    """Tests for runtime tool-home isolation."""

    def test_gradle_user_home_is_under_worktree_runtime_dir(self):
        """Gradle daemon state should be scoped to the worktree."""
        worktree = Path("/path/to/worktree")

        assert get_gradle_user_home(worktree) == (
            worktree / ".issue-orchestrator" / "tool-homes" / "gradle"
        )

    def test_build_runtime_tool_env_preserves_base_and_overrides_gradle_home(self):
        """Tool env should preserve existing variables while isolating Gradle."""
        worktree = Path("/path/to/worktree")
        env = build_runtime_tool_env(
            worktree,
            base_env={
                "PATH": "/bin",
                GRADLE_USER_HOME_ENV: "/shared/gradle",
            },
        )

        assert env["PATH"] == f"{worktree / '.venv' / 'bin'}{os.pathsep}/bin"
        assert env[GRADLE_USER_HOME_ENV] == str(get_gradle_user_home(worktree))

    def test_build_runtime_tool_env_prepends_worktree_venv_bin(self, tmp_path):
        """Validation commands should resolve tools from the worktree venv."""
        worktree = tmp_path / "worktree"
        (worktree / ".venv" / "bin").mkdir(parents=True)

        env = build_runtime_tool_env(worktree, base_env={"PATH": "/bin"})

        assert env["PATH"] == f"{worktree / '.venv' / 'bin'}{os.pathsep}/bin"

    def test_build_runtime_tool_env_empty_base_preserves_process_path(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Override-only envs still need the ambient PATH to find provider CLIs."""
        worktree = tmp_path / "worktree"
        monkeypatch.setenv("PATH", "/usr/local/bin:/bin")

        env = build_runtime_tool_env(worktree, base_env={})

        assert env["PATH"] == (
            f"{worktree / '.venv' / 'bin'}{os.pathsep}/usr/local/bin:/bin"
        )

    def test_build_runtime_tool_env_assignments_quotes_spaces(self):
        """Shell assignments should be safe for paths containing spaces."""
        worktree = Path("/path/with spaces/worktree")

        assignments = build_runtime_tool_env_assignments(worktree)

        assert assignments == [
            f"{GRADLE_USER_HOME_ENV}="
            "'/path/with spaces/worktree/.issue-orchestrator/tool-homes/gradle'",
            "PATH='/path/with spaces/worktree/.venv/bin':$PATH",
        ]

    def test_build_runtime_tool_env_assignments_prepends_worktree_venv(self, tmp_path):
        """Session launch exports should expose the worktree venv on PATH."""
        worktree = tmp_path / "worktree with spaces"

        assignments = build_runtime_tool_env_assignments(worktree)

        assert assignments == [
            f"{GRADLE_USER_HOME_ENV}="
            f"{shlex.quote(str(get_gradle_user_home(worktree)))}",
            f"PATH={shlex.quote(str(worktree / '.venv' / 'bin'))}:$PATH",
        ]


class TestBuildGitSafeCommands:
    """Tests for building git-safe export commands."""

    def test_builds_export_for_each_var(self):
        """Test that export command is built for each git-safe var."""
        commands = build_git_safe_commands()
        assert len(commands) == len(GIT_SAFE_ENV)
        assert all(cmd.startswith("export ") for cmd in commands)

    def test_includes_git_terminal_prompt(self):
        """Test that GIT_TERMINAL_PROMPT export is included."""
        commands = build_git_safe_commands()
        assert any("GIT_TERMINAL_PROMPT" in cmd for cmd in commands)
        assert any('"0"' in cmd for cmd in commands)

    def test_includes_git_askpass(self):
        """Test that GIT_ASKPASS export is included."""
        commands = build_git_safe_commands()
        assert any("GIT_ASKPASS" in cmd for cmd in commands)
        assert any("/usr/bin/false" in cmd for cmd in commands)


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
        """Test that full prefix combines scrubbing, HOME isolation, and git-safe."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=True,
            git_safe=True,
        )
        assert "unset GH_TOKEN" in prefix
        assert f'export HOME="{temp_worktree}"' in prefix
        assert "GIT_TERMINAL_PROMPT" in prefix
        assert "GIT_ASKPASS" in prefix
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
            git_safe=False,
            set_ipc_socket=False,
        )
        assert prefix == ""

    def test_git_safe_included_by_default(self, temp_worktree):
        """Test that git-safe commands are included by default."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=False,
            isolate_home=False,
        )
        assert "GIT_TERMINAL_PROMPT" in prefix
        assert "GIT_ASKPASS" in prefix

    def test_git_safe_can_be_disabled(self, temp_worktree):
        """Test that git-safe commands can be disabled."""
        prefix = build_isolation_prefix(
            worktree=temp_worktree,
            isolation_mode="standard",
            scrub_env=False,
            isolate_home=False,
            git_safe=False,
        )
        assert "GIT_TERMINAL_PROMPT" not in prefix
        assert "GIT_ASKPASS" not in prefix


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


class TestOrchestratorSocketPath:
    """Ensure the socket path is worker-isolated under pytest-xdist (#4391)."""

    def test_default_path_has_no_worker_suffix(self):
        with patch.dict(os.environ, {}, clear=True):
            path = get_orchestrator_socket_path()
        assert path == f"/tmp/issue-orchestrator-{os.getuid()}.sock"

    def test_worker_id_is_embedded_in_path(self):
        with patch.dict(os.environ, {"PYTEST_XDIST_WORKER": "gw3"}, clear=True):
            path = get_orchestrator_socket_path()
        assert path == f"/tmp/issue-orchestrator-{os.getuid()}-gw3.sock"

    def test_distinct_workers_get_distinct_paths(self):
        with patch.dict(os.environ, {"PYTEST_XDIST_WORKER": "gw0"}, clear=True):
            path_a = get_orchestrator_socket_path()
        with patch.dict(os.environ, {"PYTEST_XDIST_WORKER": "gw1"}, clear=True):
            path_b = get_orchestrator_socket_path()
        assert path_a != path_b
