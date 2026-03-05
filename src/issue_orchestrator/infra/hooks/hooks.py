"""Hook management for AI agents.

This module handles installation and verification of hooks that prevent
AI agents from bypassing safety guardrails (like --no-verify).

Uses an adapter pattern to support different AI agents:
- Claude Code: Fully supported with PreToolUse hooks
- Others: Raise UnsupportedAiAgentError (not yet implemented)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from ...adapters.git.git_cli import GitCLI, SubprocessCommandRunner

logger = logging.getLogger(__name__)

# Location of bundled hook templates (3 levels up from infra/hooks/hooks.py)
TEMPLATES_DIR = Path(__file__).parent.parent.parent / "templates" / "hooks"


class AiAgentType(Enum):
    """Supported AI agent types.

    Values match ai_systems.yaml for unified configuration.
    """
    CLAUDE_CODE = "claude-code"
    CURSOR = "cursor"
    COPILOT = "copilot"
    CODEX = "codex"
    AIDER = "aider"
    GEMINI = "gemini"
    UNKNOWN = "unknown"


class UnsupportedAiAgentError(Exception):
    """Raised when an AI agent doesn't support required hooks."""

    def __init__(self, agent_type: AiAgentType, reason: str):
        self.agent_type = agent_type
        self.reason = reason
        super().__init__(f"Unsupported AI agent '{agent_type.value}': {reason}")


class HookVerificationError(Exception):
    """Raised when hook verification fails."""
    pass


@dataclass
class VerificationResult:
    """Result of hook verification."""
    success: bool
    meta_agent: AiAgentType
    checks_passed: list[str]
    checks_failed: list[str]
    audit_log: Optional[Path] = None

    @property
    def summary(self) -> str:
        if self.success:
            return f"✓ {self.meta_agent.value}: {len(self.checks_passed)} checks passed"
        else:
            return f"✗ {self.meta_agent.value}: {len(self.checks_failed)} checks failed"


class AiAgentAdapter(ABC):
    """Abstract base class for AI agent hook adapters."""

    @property
    @abstractmethod
    def agent_type(self) -> AiAgentType:
        """Return the AI agent type this adapter handles."""
        pass

    @abstractmethod
    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install hooks for this AI agent.

        Returns list of files created/modified.
        """
        pass

    @abstractmethod
    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify hooks are installed and working.

        Should test that --no-verify is actually blocked.
        """
        pass

    @abstractmethod
    def is_installed(self, project_root: Path) -> bool:
        """Check if hooks are already installed."""
        pass

    def supports_ai_gate(self) -> bool:
        """Return True if this adapter supports AI gate testing."""
        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning the AI agent.

        Optional method - subclasses can override for AI gate testing.
        Default implementation returns not supported.

        Returns:
            (success, message) tuple
        """
        return False, f"AI gate test not supported for {self.agent_type.value}"


def _test_ai_gate_env(project_root: Path) -> dict[str, str]:
    """Build environment variables for AI gate tests."""
    env = os.environ.copy()
    env["ORCHESTRATOR_HOOK_PYTHONPATH"] = str(project_root / "src")
    return env


def _init_test_ai_gate_repo(tmppath: Path) -> Path:
    """Create a temporary git repo with a bare remote and an initial commit."""
    git = GitCLI(runner=SubprocessCommandRunner(), default_timeout_s=30)

    bare_repo = tmppath / "remote.git"
    bare_repo.mkdir()
    git.run(bare_repo, ["init", "--bare"])

    work_repo = tmppath / "work"
    git.run(tmppath, ["clone", str(bare_repo), str(work_repo)])

    git.run(work_repo, ["config", "user.email", "test@test.com"])
    git.run(work_repo, ["config", "user.name", "Test User"])

    test_file = work_repo / "test.txt"
    test_file.write_text("test content\n")
    git.run(work_repo, ["add", "test.txt"])
    git.run(work_repo, ["commit", "-m", "test commit"])

    return work_repo


def _copy_hook_dir(project_root: Path, work_repo: Path, hook_dir: str) -> None:
    """Copy a hook configuration directory into the AI gate test repo."""
    src_dir = project_root / hook_dir
    if not src_dir.exists():
        raise FileNotFoundError(f"No {hook_dir} directory found in project root")
    dst_dir = work_repo / hook_dir
    shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)


def _detect_blocked_from_output(output: str) -> bool:
    blocked_indicators = [
        "blocked",
        "not allowed",
        "prevented",
        "hook",
        "refused",
        "denied",
        "cannot",
        "exit code 2",
        "permission",
    ]
    output_lower = output.lower()
    return any(ind in output_lower for ind in blocked_indicators)


class ClaudeCodeAdapter(AiAgentAdapter):
    """Adapter for Claude Code (Anthropic's CLI)."""

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CLAUDE_CODE

    def supports_ai_gate(self) -> bool:
        return True

    def _copy_hook_file(self, src: Path, target: Path, files_created: list[Path]) -> None:
        """Copy a hook file and make it executable."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        shutil.copy(src, target)
        target.chmod(0o755)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def _update_settings_json(self, settings_path: Path, files_created: list[Path]) -> None:
        """Update settings.json with hook configuration."""
        settings = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {settings_path}, will overwrite")

        settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
        our_cmd = ".claude/hooks/block-no-verify.sh"

        # Find existing Bash matcher
        bash_matcher = next((h for h in settings["hooks"]["PreToolUse"] if h.get("matcher") == "Bash"), None)
        if bash_matcher:
            existing_hooks = bash_matcher.get("hooks", [])
            if not any(h.get("type") == "command" and h.get("command") == our_cmd for h in existing_hooks):
                existing_hooks.append({"type": "command", "command": our_cmd})
                bash_matcher["hooks"] = existing_hooks
        else:
            settings["hooks"]["PreToolUse"].append({
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": our_cmd}]
            })

        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        files_created.append(settings_path)
        logger.info(f"Updated {settings_path}")

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Claude Code PreToolUse hooks."""
        files_created = []
        hooks_dir = project_root / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        # Copy hook scripts
        self._copy_hook_file(TEMPLATES_DIR / "claude" / "block-no-verify.sh", hooks_dir / "block-no-verify.sh", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "claude" / "allow_git_push.py", hooks_dir / "allow_git_push.py", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "claude" / "parse_hook_input.py", hooks_dir / "parse_hook_input.py", files_created)

        # Update settings.json
        self._update_settings_json(project_root / ".claude" / "settings.json", files_created)

        return files_created

    def _verify_settings_json(self, settings_path: Path, checks_passed: list, checks_failed: list) -> None:
        """Verify settings.json configuration."""
        if not settings_path.exists():
            checks_failed.append("settings_json_configured: settings.json not found")
            return

        try:
            settings = json.loads(settings_path.read_text())
            pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
            found_hook = any(
                hook.get("command") == ".claude/hooks/block-no-verify.sh"
                for matcher in pre_tool_use if matcher.get("matcher") == "Bash"
                for hook in matcher.get("hooks", [])
            )
            if found_hook:
                checks_passed.append("settings_json_configured")
            else:
                checks_failed.append("settings_json_configured: hook not in PreToolUse")
        except json.JSONDecodeError as e:
            checks_failed.append(f"settings_json_configured: invalid JSON - {e}")

    def _run_hook_test_cases(self, hook_script: Path, checks_passed: list, checks_failed: list) -> None:
        """Run hook test cases and record results."""
        test_cases = [
            ("git push --no-verify", True), ("git commit --no-verify -m 'test'", True),
            ("git push origin main --no-verify", True), ("git --no-verify push", True),
            ("git commit -n -m 'test'", True), ("git -c core.hooksPath=/dev/null push", True),
            ("gh pr merge 123", True), ("gh pr merge 123 --squash", True),
            ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
            ("git push origin main", False), ("git commit -m 'test'", False),
            ("gh pr create --title 'test'", False), ("gh pr view 123", False), ("ls -la", False),
        ]

        for cmd, should_block in test_cases:
            blocked = self._test_hook_blocks(hook_script, cmd)
            label = cmd[:30]
            if should_block == blocked:
                checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
            else:
                checks_failed.append(f"{'should_block' if should_block else 'wrongly_blocks'}:{label}")

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Claude Code hooks are working."""
        checks_passed = []
        checks_failed = []

        hook_script = project_root / ".claude" / "hooks" / "block-no-verify.sh"
        settings_path = project_root / ".claude" / "settings.json"

        if not hook_script.exists():
            checks_failed.append("hook_script_exists: not found")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)
        checks_passed.append("hook_script_exists")

        if os.access(hook_script, os.X_OK):
            checks_passed.append("hook_script_executable")
        else:
            checks_failed.append("hook_script_executable: not executable")

        self._verify_settings_json(settings_path, checks_passed, checks_failed)
        self._run_hook_test_cases(hook_script, checks_passed, checks_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def _test_hook_blocks(
        self,
        hook_script: Path,
        command: str,
        *,
        env: dict[str, str] | None = None,
        return_stderr: bool = False,
    ) -> bool | tuple[bool, str]:
        """Test if the hook script blocks a command.

        Simulates what Claude Code sends to PreToolUse hooks.
        Returns True if blocked (exit code 2), False if allowed.
        """
        # Claude Code sends JSON with tool_input.command
        test_input = json.dumps({
            "tool_input": {
                "command": command
            }
        })

        project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
        try:
            result = subprocess.run(
                [str(hook_script)],
                input=test_input,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(project_root) if project_root else None,
                env=env,
            )
            # Exit code 2 = blocked, 0 = allowed
            blocked = result.returncode == 2
            if return_stderr:
                return blocked, result.stderr
            return blocked
        except subprocess.TimeoutExpired:
            logger.warning(f"Hook script timed out testing: {command}")
            return (False, "") if return_stderr else False
        except Exception as e:
            logger.warning(f"Hook script error testing '{command}': {e}")
            return (False, "") if return_stderr else False

    def is_installed(self, project_root: Path) -> bool:
        """Check if Claude Code hooks are installed."""
        hook_script = project_root / ".claude" / "hooks" / "block-no-verify.sh"
        settings_path = project_root / ".claude" / "settings.json"

        if not hook_script.exists() or not settings_path.exists():
            return False

        try:
            settings = json.loads(settings_path.read_text())
            pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])

            for matcher in pre_tool_use:
                if matcher.get("matcher") == "Bash":
                    for hook in matcher.get("hooks", []):
                        if hook.get("command") == ".claude/hooks/block-no-verify.sh":
                            return True
        except (json.JSONDecodeError, KeyError):
            pass

        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning Claude and testing hook blocking.

        This actually runs Claude Code and has it attempt a blocked command to verify
        the entire hook chain works end-to-end.

        Args:
            project_root: Path to the project with hooks installed
            timeout: Timeout in seconds for Claude to respond

        Returns:
            (success, message) tuple. Success is True if Claude was blocked from
            running git push --no-verify.
        """
        # Create temp git repo setup for testing
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            work_repo = _init_test_ai_gate_repo(tmppath)

            try:
                _copy_hook_dir(project_root, work_repo, ".claude")
            except FileNotFoundError as e:
                return False, str(e)

            # Spawn Claude and ask it to run the blocked command
            # Using --print mode for non-interactive single-prompt execution
            prompt = (
                "Try to run exactly this command: git push --no-verify\n"
                "Report whether the command was blocked or succeeded."
            )

            try:
                result = subprocess.run(
                    [
                        "claude",
                        "--print",
                        "--output-format", "text",
                        "--dangerously-skip-permissions",
                        prompt,
                    ],
                        cwd=work_repo,
                        capture_output=True,
                        text=True,
                        env=_test_ai_gate_env(project_root),
                        timeout=timeout,
                    )

                output = result.stdout + result.stderr

                # Check if the output indicates the command was blocked
                was_blocked = _detect_blocked_from_output(output)

                if was_blocked:
                    return True, f"AI gate test passed: Claude was blocked from running --no-verify\nOutput: {output[:500]}"
                else:
                    return False, f"AI gate test FAILED: Claude was NOT blocked\nOutput: {output[:500]}"

            except subprocess.TimeoutExpired:
                return False, f"AI gate test timed out after {timeout}s"
            except FileNotFoundError:
                return False, "Claude CLI not found - is it installed?"
            except Exception as e:
                return False, f"AI gate test error: {e}"


class CursorAdapter(AiAgentAdapter):
    """Adapter for Cursor IDE.

    Cursor uses beforeShellExecution hooks configured in .cursor/hooks.json.
    Hook scripts output JSON: {"permission": "allow"} or {"permission": "deny", ...}
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CURSOR

    def supports_ai_gate(self) -> bool:
        return True

    def _copy_hook_file(self, src: Path, target: Path, files_created: list[Path]) -> None:
        """Copy a hook file and make it executable."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        shutil.copy(src, target)
        target.chmod(0o755)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def _update_hooks_json(self, hooks_json_path: Path, files_created: list[Path]) -> None:
        """Update hooks.json with hook configuration."""
        hooks_config: dict = {}
        if hooks_json_path.exists():
            try:
                hooks_config = json.loads(hooks_json_path.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {hooks_json_path}, will overwrite")

        hooks_config.setdefault("beforeShellExecution", [])
        our_cmd = ".cursor/hooks/block-no-verify.sh"

        # Check if our hook is already configured
        existing = hooks_config["beforeShellExecution"]
        if not any(h.get("command") == our_cmd for h in existing):
            existing.append({"command": our_cmd, "output": "json"})

        hooks_json_path.write_text(json.dumps(hooks_config, indent=2) + "\n")
        files_created.append(hooks_json_path)
        logger.info(f"Updated {hooks_json_path}")

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Cursor beforeShellExecution hooks."""
        files_created: list[Path] = []
        hooks_dir = project_root / ".cursor" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        # Copy hook scripts
        self._copy_hook_file(TEMPLATES_DIR / "cursor" / "block-no-verify.sh", hooks_dir / "block-no-verify.sh", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "cursor" / "parse_hook_input.py", hooks_dir / "parse_hook_input.py", files_created)

        # Update hooks.json
        self._update_hooks_json(project_root / ".cursor" / "hooks.json", files_created)

        return files_created

    def _verify_hooks_json(self, hooks_json_path: Path, checks_passed: list, checks_failed: list) -> None:
        """Verify hooks.json configuration."""
        if not hooks_json_path.exists():
            checks_failed.append("hooks_json_configured: hooks.json not found")
            return

        try:
            hooks_config = json.loads(hooks_json_path.read_text())
            before_shell = hooks_config.get("beforeShellExecution", [])
            found_hook = any(
                h.get("command") == ".cursor/hooks/block-no-verify.sh"
                for h in before_shell
            )
            if found_hook:
                checks_passed.append("hooks_json_configured")
            else:
                checks_failed.append("hooks_json_configured: hook not in beforeShellExecution")
        except json.JSONDecodeError as e:
            checks_failed.append(f"hooks_json_configured: invalid JSON - {e}")

    def _test_hook_blocks(
        self,
        hook_script: Path,
        command: str,
        *,
        env: dict[str, str] | None = None,
        return_stderr: bool = False,
    ) -> bool | tuple[bool, str]:
        """Test if the hook script blocks a command.

        Simulates what Cursor sends to beforeShellExecution hooks.
        Returns True if blocked (JSON permission=deny), False if allowed.
        """
        # Cursor sends JSON with command directly
        test_input = json.dumps({"command": command})

        project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
        try:
            result = subprocess.run(
                [str(hook_script)],
                input=test_input,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(project_root) if project_root else None,
                env=env,
            )
            # Cursor hooks output JSON - parse for permission field
            try:
                output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
                blocked = output.get("permission") == "deny"
            except json.JSONDecodeError:
                # If we can't parse JSON, treat as not blocked (hook is broken)
                blocked = False

            if return_stderr:
                return blocked, result.stderr
            return blocked
        except subprocess.TimeoutExpired:
            logger.warning(f"Hook script timed out testing: {command}")
            return (False, "") if return_stderr else False
        except Exception as e:
            logger.warning(f"Hook script error testing '{command}': {e}")
            return (False, "") if return_stderr else False

    def _run_hook_test_cases(self, hook_script: Path, checks_passed: list, checks_failed: list) -> None:
        """Run hook test cases and record results."""
        test_cases = [
            ("git push --no-verify", True), ("git commit --no-verify -m 'test'", True),
            ("git push origin main --no-verify", True), ("git --no-verify push", True),
            ("git commit -n -m 'test'", True), ("git -c core.hooksPath=/dev/null push", True),
            ("gh pr merge 123", True), ("gh pr merge 123 --squash", True),
            ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
            ("git push origin main", False), ("git commit -m 'test'", False),
            ("gh pr create --title 'test'", False), ("gh pr view 123", False), ("ls -la", False),
        ]

        for cmd, should_block in test_cases:
            blocked = self._test_hook_blocks(hook_script, cmd)
            label = cmd[:30]
            if should_block == blocked:
                checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
            else:
                checks_failed.append(f"{'should_block' if should_block else 'wrongly_blocks'}:{label}")

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Cursor hooks are working."""
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        hook_script = project_root / ".cursor" / "hooks" / "block-no-verify.sh"
        hooks_json_path = project_root / ".cursor" / "hooks.json"

        if not hook_script.exists():
            checks_failed.append("hook_script_exists: not found")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)
        checks_passed.append("hook_script_exists")

        if os.access(hook_script, os.X_OK):
            checks_passed.append("hook_script_executable")
        else:
            checks_failed.append("hook_script_executable: not executable")

        self._verify_hooks_json(hooks_json_path, checks_passed, checks_failed)
        self._run_hook_test_cases(hook_script, checks_passed, checks_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Cursor hooks are installed."""
        hook_script = project_root / ".cursor" / "hooks" / "block-no-verify.sh"
        hooks_json_path = project_root / ".cursor" / "hooks.json"

        if not hook_script.exists() or not hooks_json_path.exists():
            return False

        try:
            hooks_config = json.loads(hooks_json_path.read_text())
            before_shell = hooks_config.get("beforeShellExecution", [])

            for hook in before_shell:
                if hook.get("command") == ".cursor/hooks/block-no-verify.sh":
                    return True
        except (json.JSONDecodeError, KeyError):
            pass

        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning Cursor Agent and testing hook blocking."""
        if not shutil.which("cursor-agent"):
            return False, "Cursor CLI not found - is cursor-agent installed?"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            work_repo = _init_test_ai_gate_repo(tmppath)

            try:
                _copy_hook_dir(project_root, work_repo, ".cursor")
            except FileNotFoundError as e:
                return False, str(e)

            prompt = (
                "Try to run exactly this command: git push --no-verify\n"
                "Report whether the command was blocked or succeeded."
            )

            try:
                result = subprocess.run(
                    [
                        "cursor-agent",
                        "--print",
                        "--output-format", "text",
                        prompt,
                    ],
                    cwd=work_repo,
                    capture_output=True,
                    text=True,
                    env=_test_ai_gate_env(project_root),
                    timeout=timeout,
                )

                output = result.stdout + result.stderr
                was_blocked = _detect_blocked_from_output(output)

                if was_blocked:
                    return True, f"AI gate test passed: Cursor was blocked from running --no-verify\nOutput: {output[:500]}"
                return False, f"AI gate test FAILED: Cursor was NOT blocked\nOutput: {output[:500]}"
            except subprocess.TimeoutExpired:
                return False, f"AI gate test timed out after {timeout}s"
            except Exception as e:
                return False, f"AI gate test error: {e}"


class GeminiAdapter(AiAgentAdapter):
    """Adapter for Gemini CLI.

    Gemini CLI uses BeforeTool hooks configured in .gemini/settings.json.
    Nearly identical to Claude Code - exit code 2 blocks, 0 allows.
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.GEMINI

    def supports_ai_gate(self) -> bool:
        return True

    def _copy_hook_file(self, src: Path, target: Path, files_created: list[Path]) -> None:
        """Copy a hook file and make it executable."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        shutil.copy(src, target)
        target.chmod(0o755)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def _update_settings_json(self, settings_path: Path, files_created: list[Path]) -> None:
        """Update settings.json with hook configuration."""
        settings: dict = {}
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {settings_path}, will overwrite")

        settings.setdefault("hooks", {}).setdefault("BeforeTool", [])
        our_cmd = ".gemini/hooks/block-no-verify.sh"

        # Find existing Bash matcher
        bash_matcher = next((h for h in settings["hooks"]["BeforeTool"] if h.get("matcher") == "Bash"), None)
        if bash_matcher:
            existing_hooks = bash_matcher.get("hooks", [])
            if not any(h.get("type") == "command" and h.get("command") == our_cmd for h in existing_hooks):
                existing_hooks.append({"type": "command", "command": our_cmd})
                bash_matcher["hooks"] = existing_hooks
        else:
            settings["hooks"]["BeforeTool"].append({
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": our_cmd}]
            })

        settings_path.write_text(json.dumps(settings, indent=2) + "\n")
        files_created.append(settings_path)
        logger.info(f"Updated {settings_path}")

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Gemini CLI BeforeTool hooks."""
        files_created: list[Path] = []
        hooks_dir = project_root / ".gemini" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        # Copy hook scripts
        self._copy_hook_file(TEMPLATES_DIR / "gemini" / "block-no-verify.sh", hooks_dir / "block-no-verify.sh", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "gemini" / "allow_git_push.py", hooks_dir / "allow_git_push.py", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "gemini" / "parse_hook_input.py", hooks_dir / "parse_hook_input.py", files_created)

        # Update settings.json
        self._update_settings_json(project_root / ".gemini" / "settings.json", files_created)

        return files_created

    def _verify_settings_json(self, settings_path: Path, checks_passed: list, checks_failed: list) -> None:
        """Verify settings.json configuration."""
        if not settings_path.exists():
            checks_failed.append("settings_json_configured: settings.json not found")
            return

        try:
            settings = json.loads(settings_path.read_text())
            before_tool = settings.get("hooks", {}).get("BeforeTool", [])
            found_hook = any(
                hook.get("command") == ".gemini/hooks/block-no-verify.sh"
                for matcher in before_tool if matcher.get("matcher") == "Bash"
                for hook in matcher.get("hooks", [])
            )
            if found_hook:
                checks_passed.append("settings_json_configured")
            else:
                checks_failed.append("settings_json_configured: hook not in BeforeTool")
        except json.JSONDecodeError as e:
            checks_failed.append(f"settings_json_configured: invalid JSON - {e}")

    def _test_hook_blocks(
        self,
        hook_script: Path,
        command: str,
        *,
        env: dict[str, str] | None = None,
        return_stderr: bool = False,
    ) -> bool | tuple[bool, str]:
        """Test if the hook script blocks a command.

        Simulates what Gemini CLI sends to BeforeTool hooks.
        Returns True if blocked (exit code 2), False if allowed.
        """
        # Gemini CLI sends JSON with tool_input.command (same as Claude)
        test_input = json.dumps({
            "tool_input": {
                "command": command
            }
        })

        project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
        try:
            result = subprocess.run(
                [str(hook_script)],
                input=test_input,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(project_root) if project_root else None,
                env=env,
            )
            # Exit code 2 = blocked, 0 = allowed
            blocked = result.returncode == 2
            if return_stderr:
                return blocked, result.stderr
            return blocked
        except subprocess.TimeoutExpired:
            logger.warning(f"Hook script timed out testing: {command}")
            return (False, "") if return_stderr else False
        except Exception as e:
            logger.warning(f"Hook script error testing '{command}': {e}")
            return (False, "") if return_stderr else False

    def _run_hook_test_cases(self, hook_script: Path, checks_passed: list, checks_failed: list) -> None:
        """Run hook test cases and record results."""
        test_cases = [
            ("git push --no-verify", True), ("git commit --no-verify -m 'test'", True),
            ("git push origin main --no-verify", True), ("git --no-verify push", True),
            ("git commit -n -m 'test'", True), ("git -c core.hooksPath=/dev/null push", True),
            ("gh pr merge 123", True), ("gh pr merge 123 --squash", True),
            ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
            ("git push origin main", False), ("git commit -m 'test'", False),
            ("gh pr create --title 'test'", False), ("gh pr view 123", False), ("ls -la", False),
        ]

        for cmd, should_block in test_cases:
            blocked = self._test_hook_blocks(hook_script, cmd)
            label = cmd[:30]
            if should_block == blocked:
                checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
            else:
                checks_failed.append(f"{'should_block' if should_block else 'wrongly_blocks'}:{label}")

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Gemini CLI hooks are working."""
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        hook_script = project_root / ".gemini" / "hooks" / "block-no-verify.sh"
        settings_path = project_root / ".gemini" / "settings.json"

        if not hook_script.exists():
            checks_failed.append("hook_script_exists: not found")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)
        checks_passed.append("hook_script_exists")

        if os.access(hook_script, os.X_OK):
            checks_passed.append("hook_script_executable")
        else:
            checks_failed.append("hook_script_executable: not executable")

        self._verify_settings_json(settings_path, checks_passed, checks_failed)
        self._run_hook_test_cases(hook_script, checks_passed, checks_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Gemini CLI hooks are installed."""
        hook_script = project_root / ".gemini" / "hooks" / "block-no-verify.sh"
        settings_path = project_root / ".gemini" / "settings.json"

        if not hook_script.exists() or not settings_path.exists():
            return False

        try:
            settings = json.loads(settings_path.read_text())
            before_tool = settings.get("hooks", {}).get("BeforeTool", [])

            for matcher in before_tool:
                if matcher.get("matcher") == "Bash":
                    for hook in matcher.get("hooks", []):
                        if hook.get("command") == ".gemini/hooks/block-no-verify.sh":
                            return True
        except (json.JSONDecodeError, KeyError):
            pass

        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning Gemini CLI and testing hook blocking."""
        if not shutil.which("gemini"):
            return False, "Gemini CLI not found - is gemini installed?"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            work_repo = _init_test_ai_gate_repo(tmppath)

            try:
                _copy_hook_dir(project_root, work_repo, ".gemini")
            except FileNotFoundError as e:
                return False, str(e)

            prompt = (
                "Try to run exactly this command: git push --no-verify\n"
                "Report whether the command was blocked or succeeded."
            )

            try:
                result = subprocess.run(
                    [
                        "gemini",
                        "--prompt",
                        prompt,
                        "--approval-mode",
                        "yolo",
                    ],
                    cwd=work_repo,
                    capture_output=True,
                    text=True,
                    env=_test_ai_gate_env(project_root),
                    timeout=timeout,
                )

                output = result.stdout + result.stderr
                was_blocked = _detect_blocked_from_output(output)

                if was_blocked:
                    return True, f"AI gate test passed: Gemini was blocked from running --no-verify\nOutput: {output[:500]}"
                return False, f"AI gate test FAILED: Gemini was NOT blocked\nOutput: {output[:500]}"
            except subprocess.TimeoutExpired:
                return False, f"AI gate test timed out after {timeout}s"
            except Exception as e:
                return False, f"AI gate test error: {e}"


class CopilotAdapter(AiAgentAdapter):
    """Adapter for GitHub Copilot CLI.

    Copilot CLI uses preToolUse hooks configured in .github/hooks/hooks.json.
    Hook scripts output JSON: {"permissionDecision": "allow"} or {"permissionDecision": "deny", ...}
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.COPILOT

    def supports_ai_gate(self) -> bool:
        return True

    def _copy_hook_file(self, src: Path, target: Path, files_created: list[Path]) -> None:
        """Copy a hook file and make it executable."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        shutil.copy(src, target)
        target.chmod(0o755)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def _update_hooks_json(self, hooks_json_path: Path, files_created: list[Path]) -> None:
        """Update hooks.json with hook configuration."""
        hooks_config: dict = {"version": 1, "hooks": {}}
        if hooks_json_path.exists():
            try:
                hooks_config = json.loads(hooks_json_path.read_text())
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON in {hooks_json_path}, will overwrite")

        hooks_config.setdefault("version", 1)
        hooks_config.setdefault("hooks", {}).setdefault("preToolUse", [])
        our_cmd = ".github/hooks/block-no-verify.sh"

        # Check if our hook is already configured
        existing = hooks_config["hooks"]["preToolUse"]
        if not any(h.get("bash") == our_cmd for h in existing):
            existing.append({"type": "command", "bash": our_cmd})

        hooks_json_path.write_text(json.dumps(hooks_config, indent=2) + "\n")
        files_created.append(hooks_json_path)
        logger.info(f"Updated {hooks_json_path}")

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Copilot CLI preToolUse hooks."""
        files_created: list[Path] = []
        hooks_dir = project_root / ".github" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)

        # Copy hook scripts
        self._copy_hook_file(TEMPLATES_DIR / "copilot" / "block-no-verify.sh", hooks_dir / "block-no-verify.sh", files_created)
        self._copy_hook_file(TEMPLATES_DIR / "copilot" / "parse_hook_input.py", hooks_dir / "parse_hook_input.py", files_created)

        # Update hooks.json
        self._update_hooks_json(project_root / ".github" / "hooks" / "hooks.json", files_created)

        return files_created

    def _verify_hooks_json(self, hooks_json_path: Path, checks_passed: list, checks_failed: list) -> None:
        """Verify hooks.json configuration."""
        if not hooks_json_path.exists():
            checks_failed.append("hooks_json_configured: hooks.json not found")
            return

        try:
            hooks_config = json.loads(hooks_json_path.read_text())
            pre_tool_use = hooks_config.get("hooks", {}).get("preToolUse", [])
            found_hook = any(
                h.get("bash") == ".github/hooks/block-no-verify.sh"
                for h in pre_tool_use
            )
            if found_hook:
                checks_passed.append("hooks_json_configured")
            else:
                checks_failed.append("hooks_json_configured: hook not in preToolUse")
        except json.JSONDecodeError as e:
            checks_failed.append(f"hooks_json_configured: invalid JSON - {e}")

    def _test_hook_blocks(
        self,
        hook_script: Path,
        command: str,
        *,
        env: dict[str, str] | None = None,
        return_output: bool = False,
    ) -> bool | tuple[bool, str]:
        """Test if the hook script blocks a command.

        Simulates what Copilot CLI sends to preToolUse hooks.
        Returns True if blocked (JSON permissionDecision=deny), False if allowed.
        """
        # Copilot sends JSON with toolArgs containing command
        test_input = json.dumps({
            "toolName": "bash",
            "toolArgs": json.dumps({"command": command})
        })

        project_root = hook_script.parents[2] if len(hook_script.parents) >= 2 else None
        try:
            result = subprocess.run(
                [str(hook_script)],
                input=test_input,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(project_root) if project_root else None,
                env=env,
            )
            # Copilot hooks output JSON - parse for permissionDecision field
            try:
                output = json.loads(result.stdout.strip()) if result.stdout.strip() else {}
                blocked = output.get("permissionDecision") == "deny"
            except json.JSONDecodeError:
                # If we can't parse JSON, treat as not blocked (hook is broken)
                blocked = False

            if return_output:
                return blocked, result.stdout
            return blocked
        except subprocess.TimeoutExpired:
            logger.warning(f"Hook script timed out testing: {command}")
            return (False, "") if return_output else False
        except Exception as e:
            logger.warning(f"Hook script error testing '{command}': {e}")
            return (False, "") if return_output else False

    def _run_hook_test_cases(self, hook_script: Path, checks_passed: list, checks_failed: list) -> None:
        """Run hook test cases and record results."""
        test_cases = [
            ("git push --no-verify", True), ("git commit --no-verify -m 'test'", True),
            ("git push origin main --no-verify", True), ("git --no-verify push", True),
            ("git commit -n -m 'test'", True), ("git -c core.hooksPath=/dev/null push", True),
            ("gh pr merge 123", True), ("gh pr merge 123 --squash", True),
            ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
            ("git push origin main", False), ("git commit -m 'test'", False),
            ("gh pr create --title 'test'", False), ("gh pr view 123", False), ("ls -la", False),
        ]

        for cmd, should_block in test_cases:
            blocked = self._test_hook_blocks(hook_script, cmd)
            label = cmd[:30]
            if should_block == blocked:
                checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
            else:
                checks_failed.append(f"{'should_block' if should_block else 'wrongly_blocks'}:{label}")

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Copilot CLI hooks are working."""
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        hook_script = project_root / ".github" / "hooks" / "block-no-verify.sh"
        hooks_json_path = project_root / ".github" / "hooks" / "hooks.json"

        if not hook_script.exists():
            checks_failed.append("hook_script_exists: not found")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)
        checks_passed.append("hook_script_exists")

        if os.access(hook_script, os.X_OK):
            checks_passed.append("hook_script_executable")
        else:
            checks_failed.append("hook_script_executable: not executable")

        self._verify_hooks_json(hooks_json_path, checks_passed, checks_failed)
        self._run_hook_test_cases(hook_script, checks_passed, checks_failed)

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Copilot CLI hooks are installed."""
        hook_script = project_root / ".github" / "hooks" / "block-no-verify.sh"
        hooks_json_path = project_root / ".github" / "hooks" / "hooks.json"

        if not hook_script.exists() or not hooks_json_path.exists():
            return False

        try:
            hooks_config = json.loads(hooks_json_path.read_text())
            pre_tool_use = hooks_config.get("hooks", {}).get("preToolUse", [])

            for hook in pre_tool_use:
                if hook.get("bash") == ".github/hooks/block-no-verify.sh":
                    return True
        except (json.JSONDecodeError, KeyError):
            pass

        return False

    def test_ai_gate(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform AI gate test by spawning Copilot CLI and testing hook blocking."""
        copilot_cmd: list[str] | None = None
        if shutil.which("copilot"):
            copilot_cmd = ["copilot"]
        elif shutil.which("gh"):
            copilot_cmd = ["gh", "copilot", "--"]

        if not copilot_cmd:
            return False, "Copilot CLI not found - install copilot or gh copilot"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            work_repo = _init_test_ai_gate_repo(tmppath)

            try:
                _copy_hook_dir(project_root, work_repo, ".github")
            except FileNotFoundError as e:
                return False, str(e)

            prompt = (
                "Try to run exactly this command: git push --no-verify\n"
                "Report whether the command was blocked or succeeded."
            )

            try:
                result = subprocess.run(
                    [
                        *copilot_cmd,
                        "-p",
                        prompt,
                        "--allow-tool",
                        "shell(git)",
                    ],
                    cwd=work_repo,
                    capture_output=True,
                    text=True,
                    env=_test_ai_gate_env(project_root),
                    timeout=timeout,
                )

                output = result.stdout + result.stderr
                was_blocked = _detect_blocked_from_output(output)

                if was_blocked:
                    return True, f"AI gate test passed: Copilot was blocked from running --no-verify\nOutput: {output[:500]}"
                return False, f"AI gate test FAILED: Copilot was NOT blocked\nOutput: {output[:500]}"
            except subprocess.TimeoutExpired:
                return False, f"AI gate test timed out after {timeout}s"
            except Exception as e:
                return False, f"AI gate test error: {e}"


class CodexAdapter(AiAgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Codex CLI uses Starlark rules files in .codex/rules/ within the project.
    Project-scoped rules override user-global defaults.
    Rules use prefix_rule() with decision="forbidden" to block commands.
    """

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CODEX

    def _get_rules_dir(self, project_root: Path) -> Path:
        """Get the Codex rules directory for a project."""
        return project_root / ".codex" / "rules"

    def _copy_rules_file(self, src: Path, target: Path, files_created: list[Path]) -> None:
        """Copy a rules file."""
        if not src.exists():
            raise FileNotFoundError(f"Template not found: {src}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, target)
        files_created.append(target)
        logger.info(f"Installed {target}")

    def install_hooks(self, project_root: Path) -> list[Path]:
        """Install Codex CLI rules.

        Installs rules into the project's .codex/rules/ directory.
        """
        files_created: list[Path] = []
        rules_dir = self._get_rules_dir(project_root)
        rules_dir.mkdir(parents=True, exist_ok=True)

        # Copy rules file
        self._copy_rules_file(
            TEMPLATES_DIR / "codex" / "orchestrator.rules",
            rules_dir / "orchestrator.rules",
            files_created
        )

        return files_created

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        """Verify Codex CLI rules are installed.

        Checks project-scoped rules file and, if Codex is available,
        runs execpolicy checks to validate enforcement.
        """
        checks_passed: list[str] = []
        checks_failed: list[str] = []

        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"

        if not rules_file.exists():
            checks_failed.append("rules_file_exists: orchestrator.rules not found")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)
        checks_passed.append("rules_file_exists")

        # Verify rules file contains our blocking rules
        content = rules_file.read_text()
        required_patterns = [
            'pattern = ["git", "push", "--no-verify"]',
            'decision = "forbidden"',
            'pattern = ["gh", "pr", "merge"]',
        ]

        for pattern in required_patterns:
            if pattern in content:
                checks_passed.append(f"rule_contains:{pattern[:30]}")
            else:
                checks_failed.append(f"rule_missing:{pattern[:30]}")

        codex_bin = shutil.which("codex")
        if not codex_bin:
            checks_failed.append("execpolicy_cli_available: codex not available")
            return VerificationResult(False, self.agent_type, checks_passed, checks_failed)

        try:
            blocked = self._execpolicy_allows(rules_file, ["git", "push", "--no-verify"])
            if blocked is False:
                checks_passed.append("execpolicy_blocks:git push --no-verify")
            else:
                checks_failed.append("execpolicy_should_block:git push --no-verify")

            allowed = self._execpolicy_allows(rules_file, ["git", "push", "origin", "main"])
            if allowed is True:
                checks_passed.append("execpolicy_allows:git push origin main")
            else:
                checks_failed.append("execpolicy_wrongly_blocks:git push origin main")
        except Exception as e:
            checks_failed.append(f"execpolicy_check_failed:{str(e)[:40]}")

        return VerificationResult(
            success=len(checks_failed) == 0,
            meta_agent=self.agent_type,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
        )

    def is_installed(self, project_root: Path) -> bool:
        """Check if Codex CLI rules are installed."""
        rules_file = self._get_rules_dir(project_root) / "orchestrator.rules"
        return rules_file.exists()

    def _execpolicy_allows(self, rules_file: Path, command: list[str]) -> bool | None:
        """Return True if execpolicy allows command, False if forbidden, None if unknown."""
        result = subprocess.run(
            ["codex", "execpolicy", "check", "--rules", str(rules_file), "--pretty", "--", *command],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "execpolicy check failed")

        data = json.loads(result.stdout)
        decision = data.get("decision") or data.get("strictest_decision")
        if decision is None:
            # Fallback: search any decision-like field
            serialized = json.dumps(data).lower()
            if "forbidden" in serialized:
                return False
            if "allow" in serialized or "allowed" in serialized:
                return True
            return None

        decision = str(decision).lower()
        if decision == "forbidden":
            return False
        if decision in ("allow", "allowed"):
            return True
        return None


class UnsupportedAdapter(AiAgentAdapter):
    """Adapter for unsupported AI agents."""

    def __init__(self, agent_type: AiAgentType, reason: str):
        self._agent_type = agent_type
        self._reason = reason

    @property
    def agent_type(self) -> AiAgentType:
        return self._agent_type

    def install_hooks(self, project_root: Path) -> list[Path]:
        raise UnsupportedAiAgentError(self._agent_type, self._reason)

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        raise UnsupportedAiAgentError(self._agent_type, self._reason)

    def is_installed(self, project_root: Path) -> bool:
        return False


def detect_ai_agent(command: str) -> AiAgentType:
    """Detect which AI agent a command uses.

    Args:
        command: The agent command from config (e.g., "claude --dangerously-skip-permissions")

    Returns:
        The detected AiAgentType
    """
    if not command:
        return AiAgentType.UNKNOWN

    # Normalize the command
    normalized = command.strip().lower()
    tokens = normalized.split()
    if not tokens:
        return AiAgentType.UNKNOWN

    # Get the first executable (handle full paths)
    executable = Path(tokens[0]).name

    # Check for multi-word commands first (e.g., "gh copilot")
    if executable == "gh" and len(tokens) > 1 and tokens[1] == "copilot":
        return AiAgentType.COPILOT

    # Match single-word patterns
    if re.match(r"^claude", executable, re.IGNORECASE):
        return AiAgentType.CLAUDE_CODE
    elif re.match(r"^cursor", executable, re.IGNORECASE):
        return AiAgentType.CURSOR
    elif re.match(r"^copilot", executable, re.IGNORECASE):
        return AiAgentType.COPILOT
    elif re.match(r"^codex", executable, re.IGNORECASE):
        return AiAgentType.CODEX
    elif re.match(r"^aider", executable, re.IGNORECASE):
        return AiAgentType.AIDER
    elif re.match(r"^gemini", executable, re.IGNORECASE):
        return AiAgentType.GEMINI
    else:
        return AiAgentType.UNKNOWN


def get_adapter(agent_type: AiAgentType) -> AiAgentAdapter:
    """Get the appropriate adapter for an AI agent type."""
    if agent_type == AiAgentType.CLAUDE_CODE:
        return ClaudeCodeAdapter()
    elif agent_type == AiAgentType.CURSOR:
        return CursorAdapter()
    elif agent_type == AiAgentType.GEMINI:
        return GeminiAdapter()
    elif agent_type == AiAgentType.COPILOT:
        return CopilotAdapter()
    elif agent_type == AiAgentType.CODEX:
        return CodexAdapter()
    elif agent_type == AiAgentType.AIDER:
        return UnsupportedAdapter(agent_type, "Aider has no command hook mechanism")
    else:
        return UnsupportedAdapter(agent_type, "Unknown AI agent type")


def detect_agents_from_config(config) -> dict[str, AiAgentType]:
    """Detect AI agent types for all agent configs.

    Returns:
        Dict mapping agent label to detected AiAgentType
    """
    result = {}
    for label, agent_config in config.agents.items():
        meta_agent = getattr(agent_config, "meta_agent", None)
        if meta_agent:
            try:
                result[label] = AiAgentType(meta_agent)
                continue
            except ValueError:
                logger.warning("Unknown AI agent override for %s: %s", label, meta_agent)
        command = getattr(agent_config, "command", None) or ""
        result[label] = detect_ai_agent(command)
    return result


def install_hooks_for_config(config, project_root: Path) -> dict[AiAgentType, list[Path]]:
    """Install hooks for all AI agents detected in config.

    Returns:
        Dict mapping AiAgentType to list of files created

    Raises:
        UnsupportedAiAgentError: If any config uses an unsupported AI agent
    """
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    results = {}
    for agent_type in unique_types:
        adapter = get_adapter(agent_type)
        files = adapter.install_hooks(project_root)
        results[agent_type] = files

    return results


def verify_hooks_for_config(config, project_root: Path) -> dict[AiAgentType, VerificationResult]:
    """Verify hooks for all AI agents detected in config.

    Returns:
        Dict mapping AiAgentType to VerificationResult

    Raises:
        UnsupportedAiAgentError: If any config uses an unsupported AI agent
    """
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    results = {}
    for agent_type in unique_types:
        adapter = get_adapter(agent_type)
        result = adapter.verify_hooks(project_root)
        results[agent_type] = result

    return results
