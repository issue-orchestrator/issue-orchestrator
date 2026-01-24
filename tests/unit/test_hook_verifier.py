"""Tests for ExecutionHookVerifier.

These tests verify the hook verification adapter:
- Cached verification returns success quickly
- Full verification checks all configured agent types
- Proper handling of unsupported AI agents
- Raise on failure behavior
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from issue_orchestrator.execution.hook_verifier import ExecutionHookVerifier
from issue_orchestrator.ports.hook_verifier import HookVerificationResult
from issue_orchestrator.infra.config import Config, DangerousConfig


class MockAiAgentType:
    """Mock AiAgentType enum for testing."""
    def __init__(self, value: str):
        self.value = value

    def __eq__(self, other):
        return self.value == other.value

    def __hash__(self):
        return hash(self.value)


class MockAdapter:
    """Mock adapter for testing hook verification."""
    def __init__(self, agent_type, installed=True, verify_success=True, checks_passed=None, checks_failed=None):
        self.agent_type = agent_type
        self._installed = installed
        self._verify_success = verify_success
        self._checks_passed = checks_passed or ["check1", "check2"]
        self._checks_failed = checks_failed or []

    def is_installed(self, repo_root: Path) -> bool:
        return self._installed

    def verify_hooks(self, repo_root: Path):
        result = Mock()
        result.success = self._verify_success
        result.checks_passed = self._checks_passed
        result.checks_failed = self._checks_failed
        return result


class TestExecutionHookVerifier:
    """Tests for ExecutionHookVerifier class."""

    @pytest.fixture
    def config(self, tmp_path):
        """Create a test config."""
        config = Config()
        config.repo_root = tmp_path
        config.dangerous = DangerousConfig()
        return config

    @pytest.mark.asyncio
    async def test_verify_cached_verification_valid(self, config, tmp_path, capsys):
        """When cached verification is valid, return success quickly without full verification."""
        verifier = ExecutionHookVerifier(config)

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check:
            mock_check.return_value = (True, "verified on 2024-12-19")

            result = await verifier.verify()

        assert result.success is True
        assert "cached" in result.message
        assert "verified on 2024-12-19" in result.message

        # Verify the print output
        captured = capsys.readouterr()
        assert "[OK] Hooks verified (cached)" in captured.out
        assert "verified on 2024-12-19" in captured.out

    @pytest.mark.asyncio
    async def test_verify_no_cached_all_hooks_verified(self, config, tmp_path, capsys):
        """When no cached verification, verify all hooks successfully."""
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("claude-code")
        mock_adapter = MockAdapter(agent_type, installed=True, verify_success=True)

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:web": agent_type}
            mock_get_adapter.return_value = mock_adapter

            result = await verifier.verify()

        assert result.success is True
        assert result.message == "verified"

        # Verify the print output
        captured = capsys.readouterr()
        assert "[OK] Hooks verified for claude-code" in captured.out

    @pytest.mark.asyncio
    async def test_verify_hooks_not_installed(self, config, tmp_path, capsys):
        """When hooks are not installed, return failure."""
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("claude-code")
        mock_adapter = MockAdapter(agent_type, installed=False)

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:web": agent_type}
            mock_get_adapter.return_value = mock_adapter

            result = await verifier.verify()

        assert result.success is False
        assert result.message == "verification failed"

        # Verify the print output
        captured = capsys.readouterr()
        assert "[ERROR] Hooks not installed for claude-code" in captured.out
        assert "Run 'issue-orchestrator setup-hooks'" in captured.out

    @pytest.mark.asyncio
    async def test_verify_hook_verification_failed(self, config, tmp_path, capsys):
        """When hook verification fails, return failure with details."""
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("claude-code")
        mock_adapter = MockAdapter(
            agent_type,
            installed=True,
            verify_success=False,
            checks_passed=[],
            checks_failed=["hook_script_missing", "settings_malformed"]
        )

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:web": agent_type}
            mock_get_adapter.return_value = mock_adapter

            result = await verifier.verify()

        assert result.success is False
        assert result.message == "verification failed"

        # Verify the print output
        captured = capsys.readouterr()
        assert "[ERROR] Hook verification failed for claude-code" in captured.out
        assert "hook_script_missing" in captured.out
        assert "settings_malformed" in captured.out

    @pytest.mark.asyncio
    async def test_verify_unsupported_agent_not_allowed(self, config, tmp_path, capsys):
        """When unsupported agent is detected and not allowed, return failure."""
        config.dangerous.allow_unsupported_agents = False
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("aider")

        def raise_unsupported(*args):
            from issue_orchestrator.infra.hooks.hooks import UnsupportedAiAgentError
            raise UnsupportedAiAgentError(agent_type, "Aider hooks are not supported")

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:backend": agent_type}
            mock_get_adapter.side_effect = raise_unsupported

            result = await verifier.verify()

        assert result.success is False
        assert result.message == "verification failed"
        assert len(result.unsupported_agents) == 1
        assert result.unsupported_agents[0][0] == "aider"

        # Verify the print output
        captured = capsys.readouterr()
        assert "[ERROR] Unsupported AI agent: aider" in captured.out
        assert "To allow unsupported agents" in captured.out

    @pytest.mark.asyncio
    async def test_verify_unsupported_agent_allowed(self, config, tmp_path, capsys):
        """When unsupported agent is detected but allowed, return success with warning."""
        config.dangerous.allow_unsupported_agents = True
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("aider")

        def raise_unsupported(*args):
            from issue_orchestrator.infra.hooks.hooks import UnsupportedAiAgentError
            raise UnsupportedAiAgentError(agent_type, "Aider hooks are not supported")

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:backend": agent_type}
            mock_get_adapter.side_effect = raise_unsupported

            result = await verifier.verify()

        # When only unsupported agents are allowed, verification succeeds
        assert result.success is True
        assert result.message == "verified"
        # Note: unsupported_agents list is not populated in success case, only in the result
        # The list is only added when verification fails

        # Verify the print output
        captured = capsys.readouterr()
        assert "[WARNING] Unsupported agent aider allowed (dangerous mode)" in captured.out

    @pytest.mark.asyncio
    async def test_verify_multiple_agent_types(self, config, tmp_path, capsys):
        """Verify multiple agent types correctly."""
        verifier = ExecutionHookVerifier(config)

        agent_type1 = MockAiAgentType("claude-code")
        agent_type2 = MockAiAgentType("cursor")

        mock_adapter1 = MockAdapter(agent_type1, installed=True, verify_success=True)
        mock_adapter2 = MockAdapter(agent_type2, installed=True, verify_success=True)

        def get_adapter_side_effect(agent_type):
            if agent_type.value == "claude-code":
                return mock_adapter1
            return mock_adapter2

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {
                "agent:web": agent_type1,
                "agent:mobile": agent_type2,
            }
            mock_get_adapter.side_effect = get_adapter_side_effect

            result = await verifier.verify()

        assert result.success is True
        assert result.message == "verified"

        # Verify both agents were checked
        captured = capsys.readouterr()
        assert "[OK] Hooks verified for claude-code" in captured.out
        assert "[OK] Hooks verified for cursor" in captured.out

    @pytest.mark.asyncio
    async def test_verify_deduplicates_agent_types(self, config, tmp_path):
        """When multiple agents use the same type, verify only once."""
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("claude-code")
        mock_adapter = MockAdapter(agent_type, installed=True, verify_success=True)

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            # Both agents use claude-code
            mock_detect.return_value = {
                "agent:web": agent_type,
                "agent:mobile": agent_type,
            }
            mock_get_adapter.return_value = mock_adapter

            result = await verifier.verify()

        assert result.success is True
        # get_adapter should be called only once for the unique type
        assert mock_get_adapter.call_count == 1

    @pytest.mark.asyncio
    async def test_verify_mixed_success_and_failure(self, config, tmp_path, capsys):
        """When some agents succeed and some fail, overall verification fails."""
        verifier = ExecutionHookVerifier(config)

        agent_type1 = MockAiAgentType("claude-code")
        agent_type2 = MockAiAgentType("cursor")

        mock_adapter1 = MockAdapter(agent_type1, installed=True, verify_success=True)
        mock_adapter2 = MockAdapter(agent_type2, installed=False)  # Not installed

        def get_adapter_side_effect(agent_type):
            if agent_type.value == "claude-code":
                return mock_adapter1
            return mock_adapter2

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {
                "agent:web": agent_type1,
                "agent:mobile": agent_type2,
            }
            mock_get_adapter.side_effect = get_adapter_side_effect

            result = await verifier.verify()

        assert result.success is False
        assert result.message == "verification failed"

        # Verify output shows both success and failure
        captured = capsys.readouterr()
        assert "[OK] Hooks verified for claude-code" in captured.out
        assert "[ERROR] Hooks not installed for cursor" in captured.out

    def test_raise_on_failure_raises_when_failed(self, config):
        """raise_on_failure raises RuntimeError when result.success is False."""
        verifier = ExecutionHookVerifier(config)
        result = HookVerificationResult(success=False, message="verification failed")

        with pytest.raises(RuntimeError) as exc_info:
            verifier.raise_on_failure(result)

        assert "Hook verification failed" in str(exc_info.value)

    def test_raise_on_failure_prints_detailed_message(self, config, capsys):
        """raise_on_failure prints detailed error message before raising."""
        verifier = ExecutionHookVerifier(config)
        result = HookVerificationResult(success=False, message="verification failed")

        with pytest.raises(RuntimeError):
            verifier.raise_on_failure(result)

        captured = capsys.readouterr()
        assert "STARTUP BLOCKED" in captured.out
        assert "Hook verification failed" in captured.out
        assert "issue-orchestrator setup-hooks" in captured.out
        assert "issue-orchestrator verify" in captured.out

    def test_raise_on_failure_does_nothing_when_success(self, config):
        """raise_on_failure does not raise when result.success is True."""
        verifier = ExecutionHookVerifier(config)
        result = HookVerificationResult(success=True, message="verified")

        # Should not raise
        verifier.raise_on_failure(result)

    @pytest.mark.asyncio
    async def test_verify_with_unsupported_and_failed_verification(self, config, tmp_path, capsys):
        """When there's both unsupported agents and failed verification, return failure."""
        config.dangerous.allow_unsupported_agents = False
        verifier = ExecutionHookVerifier(config)

        agent_type1 = MockAiAgentType("claude-code")
        agent_type2 = MockAiAgentType("aider")

        mock_adapter1 = MockAdapter(agent_type1, installed=False)  # Not installed

        def get_adapter_side_effect(agent_type):
            if agent_type.value == "claude-code":
                return mock_adapter1
            else:
                from issue_orchestrator.infra.hooks.hooks import UnsupportedAiAgentError
                raise UnsupportedAiAgentError(agent_type, "Aider not supported")

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {
                "agent:web": agent_type1,
                "agent:backend": agent_type2,
            }
            mock_get_adapter.side_effect = get_adapter_side_effect

            result = await verifier.verify()

        assert result.success is False
        assert result.message == "verification failed"
        assert len(result.unsupported_agents) == 1

        # Verify output shows both issues
        captured = capsys.readouterr()
        assert "[ERROR] Hooks not installed for claude-code" in captured.out
        assert "[ERROR] Unsupported AI agent: aider" in captured.out

    @pytest.mark.asyncio
    async def test_verify_only_unsupported_allowed_all_succeed(self, config, tmp_path, capsys):
        """When only unsupported agents exist and they're allowed, verification succeeds."""
        config.dangerous.allow_unsupported_agents = True
        verifier = ExecutionHookVerifier(config)

        agent_type = MockAiAgentType("aider")

        def raise_unsupported(*args):
            from issue_orchestrator.infra.hooks.hooks import UnsupportedAiAgentError
            raise UnsupportedAiAgentError(agent_type, "Aider not supported")

        with patch("issue_orchestrator.infra.hooks.hooks.check_verification_status") as mock_check, \
             patch("issue_orchestrator.infra.hooks.hooks.detect_agents_from_config") as mock_detect, \
             patch("issue_orchestrator.infra.hooks.hooks.get_adapter") as mock_get_adapter:

            mock_check.return_value = (False, "not verified")
            mock_detect.return_value = {"agent:backend": agent_type}
            mock_get_adapter.side_effect = raise_unsupported

            result = await verifier.verify()

        # all_verified starts True and remains True when unsupported is allowed
        assert result.success is True
        assert result.message == "verified"
