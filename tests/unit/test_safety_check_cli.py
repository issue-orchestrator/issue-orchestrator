"""Unit tests for safety check CLI integration.

These tests verify the setup-hooks and doctor commands properly integrate
with the safety check feature. They mock adapters to avoid spawning
actual AI agents, making them fast and reliable for CI.

Moved from integration tests since they mock adapter methods.
"""

import argparse
from datetime import datetime, timezone

import pytest


class TestSafetyCheckCLI:
    """Unit tests for safety check in CLI commands."""

    @pytest.fixture
    def repo_with_config(self, tmp_path):
        """Create a minimal repo with config for testing."""
        # Create config directory
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        # Create config file
        config_file = config_dir / "default.yaml"
        config_file.write_text(f"""
repo:
  name: test/test-repo

worktrees:
  base: {tmp_path / "worktrees"}

agents:
  agent:test:
    prompt: {tmp_path / "prompt.txt"}
    provider: claude-code
    model: sonnet

hooks:
  safety_check:
    interval_days: 7
""")

        # Create required directories/files
        (tmp_path / "prompt.txt").write_text("Test prompt")
        (tmp_path / "worktrees").mkdir(exist_ok=True)

        return tmp_path, config_file

    def test_setup_hooks_runs_safety_check(self, repo_with_config, monkeypatch):
        """Test that setup-hooks command runs live verification and shows results."""
        from issue_orchestrator.infra.hooks.hooks import (
            ClaudeCodeAdapter,
            VerificationResult,
            AiAgentType,
        )
        from issue_orchestrator.entrypoints.cli import cmd_setup_hooks

        repo_path, config_file = repo_with_config

        # Mock all adapter methods to avoid real hook operations
        monkeypatch.setattr(
            ClaudeCodeAdapter, "is_installed", lambda self, path: True
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter, "install_hooks", lambda self, path: []
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "verify_hooks",
            lambda self, path: VerificationResult(
                success=True,
                meta_agent=AiAgentType.CLAUDE_CODE,
                checks_passed=["hook_script"],
                checks_failed=[],
            ),
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "live_verify",
            lambda self, path, timeout=30: (True, "blocked git push --no-verify"),
        )

        # Create args namespace
        args = argparse.Namespace(
            config=str(config_file),
            target=str(repo_path),
        )

        # Run command
        exit_code = cmd_setup_hooks(args)

        assert exit_code == 0

        # Verify safety state was saved
        from issue_orchestrator.infra.safety_state import load_safety_state

        state = load_safety_state(repo_path)
        assert state.last_check is not None
        assert "claude-code" in state.last_results
        assert state.last_results["claude-code"].success is True

    def test_setup_hooks_fails_on_safety_check_failure(
        self, repo_with_config, monkeypatch
    ):
        """Test that setup-hooks returns non-zero when safety check fails."""
        from issue_orchestrator.infra.hooks.hooks import (
            ClaudeCodeAdapter,
            VerificationResult,
            AiAgentType,
        )
        from issue_orchestrator.entrypoints.cli import cmd_setup_hooks

        repo_path, config_file = repo_with_config

        # Mock adapter methods - safety check fails
        monkeypatch.setattr(
            ClaudeCodeAdapter, "is_installed", lambda self, path: True
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter, "install_hooks", lambda self, path: []
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "verify_hooks",
            lambda self, path: VerificationResult(
                success=True,
                meta_agent=AiAgentType.CLAUDE_CODE,
                checks_passed=["hook_script"],
                checks_failed=[],
            ),
        )
        monkeypatch.setattr(
            ClaudeCodeAdapter,
            "live_verify",
            lambda self, path, timeout=30: (False, "command was not blocked"),
        )

        args = argparse.Namespace(
            config=str(config_file),
            target=str(repo_path),
        )

        exit_code = cmd_setup_hooks(args)

        # Should fail because safety check failed
        assert exit_code == 1

    def test_doctor_shows_safety_check_with_cached_results(
        self, repo_with_config, monkeypatch
    ):
        """Test that doctor shows Safety Check with cached results."""
        from unittest.mock import MagicMock

        from issue_orchestrator.infra.safety_state import (
            SafetyState,
            SafetyCheckResult,
            save_safety_state,
        )
        from issue_orchestrator.infra.doctor.checks.hooks import check_hook_verification
        from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
        from issue_orchestrator.infra.hooks.hooks import AiAgentType, VerificationResult
        from issue_orchestrator.infra.config import Config

        repo_path, config_file = repo_with_config

        # Mock adapter to make hooks appear installed
        mock_adapter = MagicMock()
        mock_adapter.is_installed.return_value = True
        mock_adapter.verify_hooks.return_value = VerificationResult(
            success=True,
            meta_agent=AiAgentType.CLAUDE_CODE,
            checks_passed=["hook_script"],
            checks_failed=[],
        )
        monkeypatch.setattr(hook_checks, "get_adapter", lambda _: mock_adapter)

        # Pre-populate safety state with a recent successful check
        state = SafetyState(
            last_check=datetime.now(timezone.utc),
            last_results={
                "claude-code": SafetyCheckResult(
                    success=True,
                    message="blocked git push --no-verify",
                    timestamp=datetime.now(timezone.utc),
                ),
            },
        )
        save_safety_state(repo_path, state)

        # Load config and run doctor hook checks
        config = Config.load(config_file)
        checks = check_hook_verification(config)

        # Find the Safety Check
        safety_check = next((c for c in checks if c.name == "Safety Check"), None)

        assert safety_check is not None, "Safety Check should be in doctor output"
        assert safety_check.status == "ok"
        assert "Passed" in safety_check.detail
        assert safety_check.expandable is not None
        assert safety_check.expandable["ran"] is False  # Used cached results

    def test_doctor_shows_cached_failure_as_error(self, repo_with_config, monkeypatch):
        """Test that doctor shows cached failure as error status."""
        from unittest.mock import MagicMock

        from issue_orchestrator.infra.safety_state import (
            SafetyState,
            SafetyCheckResult,
            save_safety_state,
        )
        from issue_orchestrator.infra.doctor.checks.hooks import check_hook_verification
        from issue_orchestrator.infra.doctor.checks import hooks as hook_checks
        from issue_orchestrator.infra.hooks.hooks import AiAgentType, VerificationResult
        from issue_orchestrator.infra.config import Config

        repo_path, config_file = repo_with_config

        # Mock adapter to make hooks appear installed
        mock_adapter = MagicMock()
        mock_adapter.is_installed.return_value = True
        mock_adapter.verify_hooks.return_value = VerificationResult(
            success=True,
            meta_agent=AiAgentType.CLAUDE_CODE,
            checks_passed=["hook_script"],
            checks_failed=[],
        )
        monkeypatch.setattr(hook_checks, "get_adapter", lambda _: mock_adapter)

        # Pre-populate safety state with a recent FAILED check
        state = SafetyState(
            last_check=datetime.now(timezone.utc),
            last_results={
                "claude-code": SafetyCheckResult(
                    success=False,
                    message="command was not blocked",
                    timestamp=datetime.now(timezone.utc),
                ),
            },
        )
        save_safety_state(repo_path, state)

        config = Config.load(config_file)
        checks = check_hook_verification(config)

        safety_check = next((c for c in checks if c.name == "Safety Check"), None)

        assert safety_check is not None
        assert safety_check.status == "error"
        assert "Failed" in safety_check.detail
