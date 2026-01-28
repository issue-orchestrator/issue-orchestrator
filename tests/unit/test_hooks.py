"""Tests for the hooks module."""

import json
import shutil
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import Mock

import pytest


def run_hook_test(
    hook_script: Path,
    command: str,
    *,
    env: dict[str, str] | None = None,
    return_stderr: bool = False,
) -> bool | tuple[bool, str]:
    """Test helper: run a hook script and check if it blocks a command.

    Simulates what Claude Code sends to PreToolUse hooks.
    Returns True if blocked (exit code 2), False if allowed.
    """
    test_input = json.dumps({"tool_input": {"command": command}})
    project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
    try:
        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(project_root) if project_root else None,
            env=env,
        )
        blocked = result.returncode == 2
        if return_stderr:
            return blocked, result.stderr
        return blocked
    except subprocess.TimeoutExpired:
        return (False, "") if return_stderr else False
    except Exception:
        return (True, "") if return_stderr else True


def run_cursor_hook_test(
    hook_script: Path,
    command: str,
    *,
    env: dict[str, str] | None = None,
    return_output: bool = False,
) -> bool | tuple[bool, str]:
    """Test helper: run a Cursor hook script and check if it blocks a command.

    Simulates what Cursor sends to beforeShellExecution hooks.
    Returns True if blocked (JSON permission=deny), False if allowed.
    """
    test_input = json.dumps({"command": command})
    project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
    try:
        result = subprocess.run(
            [str(hook_script)],
            input=test_input,
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(project_root) if project_root else None,
            env=env,
        )
        # Cursor hooks output JSON - parse for permission field
        try:
            output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
            blocked = output.get("permission") == "deny"
        except json.JSONDecodeError:
            blocked = False

        if return_output:
            return blocked, result.stdout
        return blocked
    except subprocess.TimeoutExpired:
        return (False, "") if return_output else False
    except Exception:
        return (True, "") if return_output else True

from issue_orchestrator.infra.hooks.hooks import (
    AiAgentType,
    UnsupportedAiAgentError,
    VerificationResult,
    ClaudeCodeAdapter,
    CursorAdapter,
    UnsupportedAdapter,
    detect_ai_agent,
    get_adapter,
    detect_agents_from_config,
    TEMPLATES_DIR,
)


class TestAiAgentType:
    """Tests for AiAgentType enum."""

    def test_claude_code_value(self):
        assert AiAgentType.CLAUDE_CODE.value == "claude-code"

    def test_cursor_value(self):
        assert AiAgentType.CURSOR.value == "cursor"

    def test_all_types_have_values(self):
        for agent_type in AiAgentType:
            assert agent_type.value is not None


class TestDetectAiAgent:
    """Tests for detect_ai_agent function."""

    def test_detect_claude_code(self):
        assert detect_ai_agent("claude --dangerously-skip-permissions") == AiAgentType.CLAUDE_CODE

    def test_detect_claude_uppercase(self):
        assert detect_ai_agent("Claude -p prompt.md") == AiAgentType.CLAUDE_CODE

    def test_detect_cursor(self):
        assert detect_ai_agent("cursor") == AiAgentType.CURSOR

    def test_detect_aider(self):
        assert detect_ai_agent("aider --yes") == AiAgentType.AIDER

    def test_detect_unknown(self):
        assert detect_ai_agent("some-custom-tool") == AiAgentType.UNKNOWN

    def test_detect_empty_command(self):
        assert detect_ai_agent("") == AiAgentType.UNKNOWN

    def test_detect_full_path(self):
        assert detect_ai_agent("/usr/local/bin/claude --args") == AiAgentType.CLAUDE_CODE


class TestGetAdapter:
    """Tests for get_adapter function."""

    def test_get_claude_adapter(self):
        adapter = get_adapter(AiAgentType.CLAUDE_CODE)
        assert isinstance(adapter, ClaudeCodeAdapter)

    def test_get_cursor_adapter(self):
        adapter = get_adapter(AiAgentType.CURSOR)
        assert isinstance(adapter, CursorAdapter)

    def test_get_aider_adapter(self):
        adapter = get_adapter(AiAgentType.AIDER)
        assert isinstance(adapter, UnsupportedAdapter)

    def test_get_unknown_adapter(self):
        adapter = get_adapter(AiAgentType.UNKNOWN)
        assert isinstance(adapter, UnsupportedAdapter)


class TestUnsupportedAiAgentError:
    """Tests for UnsupportedAiAgentError exception."""

    def test_error_message(self):
        error = UnsupportedAiAgentError(AiAgentType.AIDER, "No hook support")
        assert "aider" in str(error)
        assert "No hook support" in str(error)

    def test_error_attributes(self):
        error = UnsupportedAiAgentError(AiAgentType.AIDER, "reason here")
        assert error.agent_type == AiAgentType.AIDER
        assert error.reason == "reason here"


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_success_summary(self):
        result = VerificationResult(
            success=True,
            meta_agent=AiAgentType.CLAUDE_CODE,
            checks_passed=["check1", "check2"],
            checks_failed=[],
        )
        assert "✓" in result.summary
        assert "2 checks passed" in result.summary

    def test_failure_summary(self):
        result = VerificationResult(
            success=False,
            meta_agent=AiAgentType.CLAUDE_CODE,
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
        assert adapter.agent_type == AiAgentType.CLAUDE_CODE

    def test_is_installed_false_when_missing(self, adapter, temp_project):
        assert not adapter.is_installed(temp_project)

    def test_install_hooks_creates_files(self, adapter, temp_project):
        files = adapter.install_hooks(temp_project)

        assert len(files) == 4
        assert (temp_project / ".claude" / "hooks" / "block-no-verify.sh").exists()
        assert (temp_project / ".claude" / "hooks" / "allow_git_push.py").exists()
        assert (temp_project / ".claude" / "hooks" / "parse_hook_input.py").exists()
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
        blocked = run_hook_test(hook_script, "git push --no-verify")
        assert blocked

    def test_hook_allows_normal_push(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        # Test that normal push is allowed
        blocked = run_hook_test(hook_script, "git push origin main")
        assert not blocked

    def test_hook_allows_dry_run_no_verify_with_flag(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        flag_path = temp_project / ".issue-orchestrator" / "allow-no-verify-dry-run"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text("allow\n")

        blocked = run_hook_test(hook_script, "git push --dry-run --no-verify")
        assert not blocked

    def test_hook_blocks_dry_run_no_verify_without_flag(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "git push --dry-run --no-verify")
        assert blocked

    def test_hook_blocks_when_python_missing(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        grep_path = shutil.which("grep")
        dirname_path = shutil.which("dirname")
        cat_path = shutil.which("cat")
        if not grep_path or not dirname_path or not cat_path:
            pytest.skip("Required binaries (grep/dirname/cat) not available to run hook test")
        grep_bin = Path(grep_path)
        dirname_bin = Path(dirname_path)
        cat_bin = Path(cat_path)
        bin_dir = temp_project / "bin"
        bin_dir.mkdir()
        (bin_dir / "grep").symlink_to(grep_bin)
        (bin_dir / "dirname").symlink_to(dirname_bin)
        (bin_dir / "cat").symlink_to(cat_bin)

        result = run_hook_test(
            hook_script,
            "git push --dry-run --no-verify",
            env={"PATH": str(bin_dir)},
            return_stderr=True,
        )
        assert isinstance(result, tuple)
        blocked, stderr = result
        assert blocked
        assert "python3 is required" in stderr.lower()

    def test_hook_blocks_when_allow_script_missing(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"
        allow_script = temp_project / ".claude" / "hooks" / "allow_git_push.py"

        allow_script.unlink()

        result = run_hook_test(
            hook_script,
            "git push --dry-run --no-verify",
            return_stderr=True,
        )
        assert isinstance(result, tuple)
        blocked, stderr = result
        assert blocked
        assert "missing" in stderr.lower()

    def test_hook_blocks_when_parse_script_missing(self, adapter, temp_project):
        """Hook must fail closed when parse_hook_input.py is missing."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"
        parse_script = temp_project / ".claude" / "hooks" / "parse_hook_input.py"

        parse_script.unlink()

        result = run_hook_test(
            hook_script,
            "git push origin main",  # Normal command that would otherwise be allowed
            return_stderr=True,
        )
        assert isinstance(result, tuple)
        blocked, stderr = result
        assert blocked, "Hook should block when parse_hook_input.py is missing (fail closed)"
        assert "missing" in stderr.lower()

    def test_hook_blocks_when_input_malformed(self, adapter, temp_project):
        """Hook must fail closed when input is non-empty but command extraction returns empty."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        # Malformed input: valid JSON but no command field
        result = subprocess.run(
            ["bash", str(hook_script)],
            input='{"unexpected_key": "value"}',
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, "Hook should block when input is malformed (fail closed)"
        assert "malformed" in result.stderr.lower()

    def test_hook_blocks_commit_no_verify(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "git commit --no-verify -m 'test'")
        assert blocked

    def test_hook_blocks_hooks_path_disable(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "git -c core.hooksPath=/dev/null push")
        assert blocked

    def test_hook_blocks_gh_pr_merge(self, adapter, temp_project):
        """Agents cannot merge PRs via gh pr merge."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "gh pr merge 123")
        assert blocked

    def test_hook_blocks_gh_pr_merge_with_flags(self, adapter, temp_project):
        """Agents cannot merge PRs via gh pr merge with flags."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "gh pr merge 123 --squash")
        assert blocked

    def test_hook_blocks_gh_api_merge(self, adapter, temp_project):
        """Agents cannot merge PRs via gh api."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "gh api repos/owner/repo/pulls/123/merge -X PUT")
        assert blocked

    def test_hook_allows_gh_pr_create(self, adapter, temp_project):
        """Agents can create PRs."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "gh pr create --title 'test'")
        assert not blocked

    def test_hook_allows_gh_pr_view(self, adapter, temp_project):
        """Agents can view PRs."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".claude" / "hooks" / "block-no-verify.sh"

        blocked = run_hook_test(hook_script, "gh pr view 123")
        assert not blocked


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
        assert adapter.agent_type == AiAgentType.CURSOR

    def test_is_installed_false_when_missing(self, adapter, temp_project):
        assert not adapter.is_installed(temp_project)

    def test_install_hooks_creates_files(self, adapter, temp_project):
        files = adapter.install_hooks(temp_project)

        assert len(files) == 3  # hook script, parse_hook_input.py, hooks.json
        assert (temp_project / ".cursor" / "hooks" / "block-no-verify.sh").exists()
        assert (temp_project / ".cursor" / "hooks" / "parse_hook_input.py").exists()
        assert (temp_project / ".cursor" / "hooks.json").exists()

    def test_install_hooks_script_is_executable(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        assert os.access(hook_script, os.X_OK)

    def test_install_hooks_json_has_correct_config(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hooks_json = temp_project / ".cursor" / "hooks.json"

        config = json.loads(hooks_json.read_text())
        assert "beforeShellExecution" in config
        hooks = config["beforeShellExecution"]
        assert any(
            h.get("command") == ".cursor/hooks/block-no-verify.sh"
            for h in hooks
        )

    def test_install_hooks_preserves_existing_config(self, adapter, temp_project):
        # Create existing hooks.json with custom config
        cursor_dir = temp_project / ".cursor"
        cursor_dir.mkdir()
        hooks_json = cursor_dir / "hooks.json"
        hooks_json.write_text(json.dumps({
            "beforeShellExecution": [{"command": "other-hook.sh", "output": "text"}],
            "customSetting": True
        }))

        adapter.install_hooks(temp_project)

        config = json.loads(hooks_json.read_text())
        assert config.get("customSetting") is True
        hooks = config["beforeShellExecution"]
        assert len(hooks) == 2
        assert any(h.get("command") == "other-hook.sh" for h in hooks)
        assert any(h.get("command") == ".cursor/hooks/block-no-verify.sh" for h in hooks)

    def test_is_installed_true_after_install(self, adapter, temp_project):
        adapter.install_hooks(temp_project)

        assert adapter.is_installed(temp_project)

    def test_verify_hooks_passes_after_install(self, adapter, temp_project):
        adapter.install_hooks(temp_project)

        result = adapter.verify_hooks(temp_project)

        assert result.success
        assert result.meta_agent == AiAgentType.CURSOR
        assert len(result.checks_failed) == 0

    def test_verify_hooks_fails_when_not_installed(self, adapter, temp_project):
        result = adapter.verify_hooks(temp_project)

        assert not result.success
        assert "hook_script_exists" in result.checks_failed[0]

    def test_hook_blocks_no_verify(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "git push --no-verify")
        assert blocked

    def test_hook_allows_normal_push(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "git push origin main")
        assert not blocked

    def test_hook_allows_dry_run_no_verify_with_flag(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        # Create the allow flag file
        flag_dir = temp_project / ".issue-orchestrator"
        flag_dir.mkdir()
        (flag_dir / "allow-no-verify-dry-run").write_text("")

        blocked = run_cursor_hook_test(hook_script, "git push --dry-run --no-verify origin main")
        assert not blocked

    def test_hook_blocks_dry_run_no_verify_without_flag(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "git push --dry-run --no-verify")
        assert blocked

    def test_hook_blocks_when_python_missing(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        grep_path = shutil.which("grep")
        dirname_path = shutil.which("dirname")
        cat_path = shutil.which("cat")
        if not grep_path or not dirname_path or not cat_path:
            pytest.skip("Required binaries (grep/dirname/cat) not available to run hook test")
        grep_bin = Path(grep_path)
        dirname_bin = Path(dirname_path)
        cat_bin = Path(cat_path)
        bin_dir = temp_project / "bin"
        bin_dir.mkdir()
        (bin_dir / "grep").symlink_to(grep_bin)
        (bin_dir / "dirname").symlink_to(dirname_bin)
        (bin_dir / "cat").symlink_to(cat_bin)

        result = run_cursor_hook_test(
            hook_script,
            "git push --dry-run --no-verify",
            env={"PATH": str(bin_dir)},
            return_output=True,
        )
        assert isinstance(result, tuple)
        blocked, output = result
        assert blocked
        assert "python3 is required" in output.lower()

    def test_hook_blocks_when_parse_script_missing(self, adapter, temp_project):
        """Hook must fail closed when parse_hook_input.py is missing."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"
        parse_script = temp_project / ".cursor" / "hooks" / "parse_hook_input.py"

        parse_script.unlink()

        result = run_cursor_hook_test(
            hook_script,
            "git push origin main",  # Normal command that would otherwise be allowed
            return_output=True,
        )
        assert isinstance(result, tuple)
        blocked, output = result
        assert blocked, "Hook should block when parse_hook_input.py is missing (fail closed)"
        assert "missing" in output.lower()

    def test_hook_blocks_when_input_malformed(self, adapter, temp_project):
        """Hook must fail closed when input is non-empty but command extraction returns empty."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        # Malformed input: valid JSON but no command field
        result = subprocess.run(
            ["bash", str(hook_script)],
            input='{"unexpected_key": "value"}',
            capture_output=True,
            text=True,
        )
        output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
        assert output.get("permission") == "deny", "Hook should block when input is malformed (fail closed)"
        assert "malformed" in output.get("userMessage", "").lower()

    def test_hook_blocks_commit_no_verify(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "git commit --no-verify -m 'test'")
        assert blocked

    def test_hook_blocks_hooks_path_disable(self, adapter, temp_project):
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "git -c core.hooksPath=/dev/null push")
        assert blocked

    def test_hook_blocks_gh_pr_merge(self, adapter, temp_project):
        """Agents cannot merge PRs via gh pr merge."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "gh pr merge 123")
        assert blocked

    def test_hook_blocks_gh_api_merge(self, adapter, temp_project):
        """Agents cannot merge PRs via gh api."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "gh api repos/owner/repo/pulls/123/merge -X PUT")
        assert blocked

    def test_hook_allows_gh_pr_create(self, adapter, temp_project):
        """Agents can create PRs."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "gh pr create --title 'test'")
        assert not blocked

    def test_hook_allows_gh_pr_view(self, adapter, temp_project):
        """Agents can view PRs."""
        adapter.install_hooks(temp_project)
        hook_script = temp_project / ".cursor" / "hooks" / "block-no-verify.sh"

        blocked = run_cursor_hook_test(hook_script, "gh pr view 123")
        assert not blocked


class TestUnsupportedAdapter:
    """Tests for UnsupportedAdapter."""

    @pytest.fixture
    def adapter(self):
        return UnsupportedAdapter(AiAgentType.AIDER, "No hook support")

    @pytest.fixture
    def temp_project(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_agent_type(self, adapter):
        assert adapter.agent_type == AiAgentType.AIDER

    def test_install_hooks_raises(self, adapter, temp_project):
        with pytest.raises(UnsupportedAiAgentError):
            adapter.install_hooks(temp_project)

    def test_verify_hooks_raises(self, adapter, temp_project):
        with pytest.raises(UnsupportedAiAgentError):
            adapter.verify_hooks(temp_project)

    def test_is_installed_always_false(self, adapter, temp_project):
        assert not adapter.is_installed(temp_project)


class TestDetectAgentsFromConfig:
    """Tests for detect_agents_from_config function."""

    def test_detects_single_agent(self):
        mock_config = Mock()
        mock_agent = Mock()
        mock_agent.command = "claude --dangerously-skip-permissions"
        mock_config.agents = {"agent:backend": mock_agent}

        result = detect_agents_from_config(mock_config)

        assert result["agent:backend"] == AiAgentType.CLAUDE_CODE

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

        assert result["agent:backend"] == AiAgentType.CLAUDE_CODE
        assert result["agent:frontend"] == AiAgentType.AIDER

    def test_handles_no_command(self):
        mock_config = Mock()
        mock_agent = Mock()
        mock_agent.command = None
        mock_config.agents = {"agent:test": mock_agent}

        result = detect_agents_from_config(mock_config)

        assert result["agent:test"] == AiAgentType.UNKNOWN


class TestParseHookInput:
    """Tests for parse_hook_input.py (extract_command function)."""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """Load extract_command from the template script."""
        import importlib.util

        script_path = TEMPLATES_DIR / "claude" / "parse_hook_input.py"
        spec = importlib.util.spec_from_file_location("parse_hook_input", script_path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.extract_command = mod.extract_command

    def test_claude_format(self):
        raw = json.dumps({"tool_input": {"command": "git push --no-verify"}})
        assert self.extract_command(raw) == "git push --no-verify"

    def test_cursor_format(self):
        raw = json.dumps({"command": "git push --no-verify"})
        assert self.extract_command(raw) == "git push --no-verify"

    def test_claude_format_takes_priority(self):
        raw = json.dumps({
            "tool_input": {"command": "git push"},
            "command": "something else",
        })
        assert self.extract_command(raw) == "git push"

    def test_empty_tool_input_falls_back_to_command(self):
        raw = json.dumps({"tool_input": {}, "command": "git status"})
        assert self.extract_command(raw) == "git status"

    def test_missing_command_returns_empty(self):
        raw = json.dumps({"tool_input": {"other": "data"}})
        assert self.extract_command(raw) == ""

    def test_empty_json_returns_empty(self):
        assert self.extract_command("{}") == ""

    def test_invalid_json_returns_empty(self):
        assert self.extract_command("not json") == ""

    def test_empty_string_returns_empty(self):
        assert self.extract_command("") == ""

    def test_non_string_command_returns_empty(self):
        raw = json.dumps({"tool_input": {"command": 42}})
        assert self.extract_command(raw) == ""


class TestTemplatesExist:
    """Tests that template files exist."""

    def test_claude_template_exists(self):
        template = TEMPLATES_DIR / "claude" / "block-no-verify.sh"
        assert template.exists(), f"Template not found: {template}"

    def test_claude_settings_template_exists(self):
        template = TEMPLATES_DIR / "claude" / "settings.json"
        assert template.exists(), f"Template not found: {template}"

    def test_claude_parse_hook_input_exists(self):
        template = TEMPLATES_DIR / "claude" / "parse_hook_input.py"
        assert template.exists(), f"Template not found: {template}"

    def test_cursor_template_exists(self):
        template = TEMPLATES_DIR / "cursor" / "block-no-verify.sh"
        assert template.exists(), f"Template not found: {template}"

    def test_cursor_hooks_json_template_exists(self):
        template = TEMPLATES_DIR / "cursor" / "hooks.json"
        assert template.exists(), f"Template not found: {template}"

    def test_cursor_parse_hook_input_exists(self):
        template = TEMPLATES_DIR / "cursor" / "parse_hook_input.py"
        assert template.exists(), f"Template not found: {template}"
