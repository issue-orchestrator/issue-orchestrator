"""Tests for the hooks module."""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from issue_orchestrator.hooks import (
    MetaAgentType,
    UnsupportedMetaAgentError,
    VerificationResult,
    VerificationMarker,
    ClaudeCodeAdapter,
    CursorAdapter,
    UnsupportedAdapter,
    detect_meta_agent,
    get_adapter,
    detect_agents_from_config,
    check_verification_status,
    TEMPLATES_DIR,
)


class TestMetaAgentType:
    """Tests for MetaAgentType enum."""

    def test_claude_code_value(self):
        assert MetaAgentType.CLAUDE_CODE.value == "claude-code"

    def test_cursor_value(self):
        assert MetaAgentType.CURSOR.value == "cursor"

    def test_all_types_have_values(self):
        for agent_type in MetaAgentType:
            assert agent_type.value is not None


class TestDetectMetaAgent:
    """Tests for detect_meta_agent function."""

    def test_detect_claude_code(self):
        assert detect_meta_agent("claude --dangerously-skip-permissions") == MetaAgentType.CLAUDE_CODE

    def test_detect_claude_uppercase(self):
        assert detect_meta_agent("Claude -p prompt.md") == MetaAgentType.CLAUDE_CODE

    def test_detect_cursor(self):
        assert detect_meta_agent("cursor") == MetaAgentType.CURSOR

    def test_detect_aider(self):
        assert detect_meta_agent("aider --yes") == MetaAgentType.AIDER

    def test_detect_unknown(self):
        assert detect_meta_agent("some-custom-tool") == MetaAgentType.UNKNOWN

    def test_detect_empty_command(self):
        assert detect_meta_agent("") == MetaAgentType.UNKNOWN

    def test_detect_full_path(self):
        assert detect_meta_agent("/usr/local/bin/claude --args") == MetaAgentType.CLAUDE_CODE


class TestGetAdapter:
    """Tests for get_adapter function."""

    def test_get_claude_adapter(self):
        adapter = get_adapter(MetaAgentType.CLAUDE_CODE)
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_get_cursor_adapter(self):
        adapter = get_adapter(MetaAgentType.CURSOR)
        assert isinstance(adapter, CursorAdapter)

    def test_get_aider_adapter(self):
        adapter = get_adapter(MetaAgentType.AIDER)
        assert isinstance(adapter, UnsupportedAdapter)

    def test_get_unknown_adapter(self):
        adapter = get_adapter(MetaAgentType.UNKNOWN)
        assert isinstance(adapter, UnsupportedAdapter)


class TestUnsupportedMetaAgentError:
    """Tests for UnsupportedMetaAgentError exception."""

    def test_error_message(self):
        error = UnsupportedMetaAgentError(MetaAgentType.AIDER, "No hook support")
        assert "aider" in str(error)
        assert "No hook support" in str(error)

    def test_error_attributes(self):
        error = UnsupportedMetaAgentError(MetaAgentType.AIDER, "reason here")
        assert error.agent_type == MetaAgentType.AIDER
        assert error.reason == "reason here"


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_success_summary(self):
        result = VerificationResult(
            success=True,
            meta_agent=MetaAgentType.CLAUDE_CODE,
            checks_passed=["check1", "check2"],
            checks_failed=[],
        )
        assert "✓" in result.summary
        assert "2 checks passed" in result.summary

    def test_failure_summary(self):
        result = VerificationResult(
            success=False,
            meta_agent=MetaAgentType.CLAUDE_CODE,
            checks_passed=[],
            checks_failed=["fail1", "fail2"],
        )
        assert "✗" in result.summary
        assert "2 checks failed" in result.summary


class TestClaudeCodeAdapter:
    """Tests for ClaudeCodeAdapter."""

    @pytest.fixture
    def adapter(self):
        return ClaudeCodeAdapter()

    @pytest.fixture
    def temp_project(self):
        """Create a temporary project directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_agent_type(self, adapter):
        assert adapter.agent_type == MetaAgentType.CLAUDE_CODE

    def test_is_installed_false_when_missing(self, adapter, temp_project):
        assert not adapter.is_installed(temp_project)

    def test_install_hooks_creates_files(self, adapter, temp_project):
        files = adapter.install_hooks(temp_project)

        assert len(files) == 2
        assert (temp_project / ".claude" / "hooks" / "block-no-verify.sh").exists()
        assert (temp_project / ".claude" / "settings.json").exists()

    def test_install_hooks_script_is_executable(self, adapter, temp_project):
        adapter.install_hooks(temp_project)

        script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"
        assert os.access(script, os.X_OK)

    def test_install_hooks_settings_has_correct_config(self, adapter, temp_project):
        adapter.install_hooks(temp_project)

        settings_path = temp_project / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())

        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]

        # Find Bash matcher
        bash_matcher = None
        for matcher in settings["hooks"]["PreToolUse"]:
            if matcher.get("matcher") == "Bash":
                bash_matcher = matcher
                break

        assert bash_matcher is not None
        assert any(
            h.get("command") == ".claude/hooks/block-no-verify.sh"
            for h in bash_matcher["hooks"]
        )

    def test_install_hooks_preserves_existing_settings(self, adapter, temp_project):
        # Create existing settings
        claude_dir = temp_project / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        settings_path.write_text(json.dumps({
            "someOtherSetting": True,
            "hooks": {
                "SomeOtherHook": [{"matcher": "Test"}]
            }
        }))

        adapter.install_hooks(temp_project)

        settings = json.loads(settings_path.read_text())
        assert settings["someOtherSetting"] is True
        assert "SomeOtherHook" in settings["hooks"]

    def test_is_installed_true_after_install(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        assert adapter.is_installed(temp_project)

    def test_verify_hooks_passes_after_install(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        result = adapter.verify_hooks(temp_project)

        assert result.success
        assert len(result.checks_passed) > 0
        assert len(result.checks_failed) == 0

    def test_verify_hooks_fails_when_not_installed(self, adapter, temp_project):
        result = adapter.verify_hooks(temp_project)

        assert not result.success
        assert "hook_script_exists" in result.checks_failed[0]

    def test_hook_blocks_no_verify(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        # Test that --no-verify is blocked
        blocked = adapter._test_hook_blocks(hook_script, "git push --no-verify")
        assert blocked

    def test_hook_allows_normal_push(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        # Test that normal push is allowed
        blocked = adapter._test_hook_blocks(hook_script, "git push origin main")
        assert not blocked

    def test_hook_blocks_commit_no_verify(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = adapter._test_hook_blocks(hook_script, "git commit --no-verify -m 'test'")
        assert blocked

    def test_hook_blocks_hooks_path_disable(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = adapter._test_hook_blocks(hook_script, "git -c core.hooksPath=/dev/null push")
        assert blocked


class TestCursorAdapter:
    """Tests for CursorAdapter."""

    @pytest.fixture
    def adapter(self):
        return CursorAdapter()

    @pytest.fixture
    def temp_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_agent_type(self, adapter):
        assert adapter.agent_type == MetaAgentType.CURSOR

    def test_install_hooks_raises_unsupported(self, adapter, temp_project):
        with pytest.raises(UnsupportedMetaAgentError) as exc_info:
            adapter.install_hooks(temp_project)

        assert exc_info.value.agent_type == MetaAgentType.CURSOR

    def test_verify_hooks_raises_unsupported(self, adapter, temp_project):
        with pytest.raises(UnsupportedMetaAgentError):
            adapter.verify_hooks(temp_project)


class TestUnsupportedAdapter:
    """Tests for UnsupportedAdapter."""

    @pytest.fixture
    def adapter(self):
        return UnsupportedAdapter(MetaAgentType.AIDER, "No hook support")

    @pytest.fixture
    def temp_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_agent_type(self, adapter):
        assert adapter.agent_type == MetaAgentType.AIDER

    def test_install_hooks_raises(self, adapter, temp_project):
        with pytest.raises(UnsupportedMetaAgentError):
            adapter.install_hooks(temp_project)

    def test_verify_hooks_raises(self, adapter, temp_project):
        with pytest.raises(UnsupportedMetaAgentError):
            adapter.verify_hooks(temp_project)

    def test_is_installed_always_false(self, adapter, temp_project):
        assert not adapter.is_installed(temp_project)


class TestVerificationMarker:
    """Tests for VerificationMarker."""

    @pytest.fixture
    def temp_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_save_and_load(self, temp_project):
        marker = VerificationMarker(
            verified_at=datetime(2024, 12, 19, 10, 30, 0),
            meta_agent=MetaAgentType.CLAUDE_CODE,
            hooks_hash="abc123",
            signature="sig456",
        )
        marker.save(temp_project)

        loaded = VerificationMarker.load(temp_project)
        assert loaded is not None
        assert loaded.meta_agent == MetaAgentType.CLAUDE_CODE
        assert loaded.hooks_hash == "abc123"

    def test_load_returns_none_when_missing(self, temp_project):
        assert VerificationMarker.load(temp_project) is None

    def test_load_returns_none_for_invalid_json(self, temp_project):
        marker_path = temp_project / VerificationMarker.MARKER_FILE
        marker_path.write_text("not valid json")

        assert VerificationMarker.load(temp_project) is None

    def test_compute_signature(self):
        marker = VerificationMarker(
            verified_at=datetime(2024, 12, 19, 10, 30, 0),
            meta_agent=MetaAgentType.CLAUDE_CODE,
            hooks_hash="abc123",
            signature="",
        )
        sig = marker.compute_signature()
        assert len(sig) == 16  # 16 hex chars

    def test_is_valid_checks_signature(self, temp_project):
        marker = VerificationMarker(
            verified_at=datetime.now(),
            meta_agent=MetaAgentType.CLAUDE_CODE,
            hooks_hash="abc123",
            signature="wrong_signature",
        )
        assert not marker.is_valid(temp_project)

    def test_compute_hooks_hash_empty_when_no_files(self, temp_project):
        hash_val = VerificationMarker.compute_hooks_hash(
            temp_project, MetaAgentType.CLAUDE_CODE
        )
        # Should return something even when files don't exist
        assert len(hash_val) > 0


class TestDetectAgentsFromConfig:
    """Tests for detect_agents_from_config function."""

    def test_detects_single_agent(self):
        mock_config = Mock()
        mock_agent = Mock()
        mock_agent.command = "claude --dangerously-skip-permissions"
        mock_config.agents = {"agent:backend": mock_agent}

        result = detect_agents_from_config(mock_config)

        assert result["agent:backend"] == MetaAgentType.CLAUDE_CODE

    def test_detects_multiple_agents(self):
        mock_config = Mock()

        agent1 = Mock()
        agent1.command = "claude -p prompt.md"

        agent2 = Mock()
        agent2.command = "aider --yes"

        mock_config.agents = {
            "agent:backend": agent1,
            "agent:frontend": agent2,
        }

        result = detect_agents_from_config(mock_config)

        assert result["agent:backend"] == MetaAgentType.CLAUDE_CODE
        assert result["agent:frontend"] == MetaAgentType.AIDER

    def test_handles_no_command(self):
        mock_config = Mock()
        mock_agent = Mock()
        mock_agent.command = None
        mock_config.agents = {"agent:test": mock_agent}

        result = detect_agents_from_config(mock_config)

        assert result["agent:test"] == MetaAgentType.UNKNOWN


class TestCheckVerificationStatus:
    """Tests for check_verification_status function."""

    @pytest.fixture
    def temp_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_config(self):
        mock_config = Mock()
        mock_agent = Mock()
        mock_agent.command = "claude"
        mock_config.agents = {"agent:test": mock_agent}
        return mock_config

    def test_returns_false_when_no_marker(self, temp_project, mock_config):
        is_valid, message = check_verification_status(temp_project, mock_config)

        assert not is_valid
        assert "not verified" in message.lower()

    def test_returns_false_when_invalid_signature(self, temp_project, mock_config):
        # Create marker with bad signature
        marker = VerificationMarker(
            verified_at=datetime.now(),
            meta_agent=MetaAgentType.CLAUDE_CODE,
            hooks_hash="abc123",
            signature="bad_sig",
        )
        marker.save(temp_project)

        is_valid, message = check_verification_status(temp_project, mock_config)

        assert not is_valid
        assert "changed" in message.lower()


class TestTemplatesExist:
    """Tests that template files exist."""

    def test_claude_template_exists(self):
        template = TEMPLATES_DIR / "claude" / "block-no-verify.sh"
        assert template.exists(), f"Template not found: {template}"

    def test_claude_settings_template_exists(self):
        template = TEMPLATES_DIR / "claude" / "settings.json"
        assert template.exists(), f"Template not found: {template}"
