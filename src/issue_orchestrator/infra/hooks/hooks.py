"""Hook management for AI agents.

This module handles installation and verification of hooks that prevent
AI agents from bypassing safety guardrails (like --no-verify).

Uses an adapter pattern to support different AI agents:
- Claude Code: Fully supported with PreToolUse hooks
- Others: Raise UnsupportedAiAgentError (not yet implemented)
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class VerificationMarker:
    """Tamper-proof marker proving verification passed."""
    verified_at: datetime
    meta_agent: AiAgentType
    hooks_hash: str
    signature: str

    # Marker lives in .issue-orchestrator/ directory
    MARKER_DIR = ".issue-orchestrator"
    MARKER_FILE = "verified"

    @classmethod
    def compute_hooks_hash(cls, project_root: Path, ai_agent: AiAgentType) -> str:
        """Compute hash of all hook files for an AI agent."""
        hasher = hashlib.sha256()

        if ai_agent == AiAgentType.CLAUDE_CODE:
            files = [
                project_root / ".claude" / "hooks" / "block-no-verify.sh",
                project_root / ".claude" / "settings.json",
            ]
        else:
            files = []

        for f in sorted(files):
            if f.exists():
                hasher.update(f.read_bytes())
                hasher.update(f.name.encode())

        return hasher.hexdigest()[:16]

    def compute_signature(self, secret: str = "orchestrator-v1") -> str:
        """Compute tamper-proof signature."""
        data = f"{self.verified_at.isoformat()}:{self.meta_agent.value}:{self.hooks_hash}:{secret}"
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def save(self, project_root: Path) -> None:
        """Save marker to .issue-orchestrator/ directory."""
        marker_dir = project_root / self.MARKER_DIR
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker_path = marker_dir / self.MARKER_FILE
        data = {
            "verified_at": self.verified_at.isoformat(),
            "meta_agent": self.meta_agent.value,
            "hooks_hash": self.hooks_hash,
            "signature": self.signature,
        }
        marker_path.write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, project_root: Path) -> Optional["VerificationMarker"]:
        """Load marker from .issue-orchestrator/ directory."""
        marker_path = project_root / cls.MARKER_DIR / cls.MARKER_FILE
        if not marker_path.exists():
            return None

        try:
            data = json.loads(marker_path.read_text())
            marker = cls(
                verified_at=datetime.fromisoformat(data["verified_at"]),
                meta_agent=AiAgentType(data["meta_agent"]),
                hooks_hash=data["hooks_hash"],
                signature=data["signature"],
            )
            return marker
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Invalid verification marker: {e}")
            return None

    def is_valid(self, project_root: Path) -> bool:
        """Check if marker is valid and hooks haven't changed."""
        # Check signature
        expected_sig = self.compute_signature()
        if self.signature != expected_sig:
            logger.warning("Verification marker signature mismatch")
            return False

        # Check hooks haven't changed
        current_hash = self.compute_hooks_hash(project_root, self.meta_agent)
        if self.hooks_hash != current_hash:
            logger.warning(f"Hooks have changed since verification (was {self.hooks_hash}, now {current_hash})")
            return False

        return True


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

    def live_verify(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform live verification by spawning the AI agent.

        Optional method - subclasses can override for live testing.
        Default implementation returns not supported.

        Returns:
            (success, message) tuple
        """
        return False, f"Live verification not supported for {self.agent_type.value}"


class ClaudeCodeAdapter(AiAgentAdapter):
    """Adapter for Claude Code (Anthropic's CLI)."""

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CLAUDE_CODE

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
                timeout=5,
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

    def live_verify(self, project_root: Path, timeout: int = 60) -> tuple[bool, str]:
        """Perform live verification by spawning Claude and testing hook blocking.

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
            git = GitCLI(runner=SubprocessCommandRunner(), default_timeout_s=30)

            # Create a bare repo to act as "remote"
            bare_repo = tmppath / "remote.git"
            bare_repo.mkdir()
            git.run(bare_repo, ["init", "--bare"])

            # Create working repo cloned from bare
            work_repo = tmppath / "work"
            git.run(tmppath, ["clone", str(bare_repo), str(work_repo)])

            # Configure git user for commit
            git.run(work_repo, ["config", "user.email", "test@test.com"])
            git.run(work_repo, ["config", "user.name", "Test User"])

            # Create a commit to push
            test_file = work_repo / "test.txt"
            test_file.write_text("test content\n")
            git.run(work_repo, ["add", "test.txt"])
            git.run(work_repo, ["commit", "-m", "test commit"])

            # Copy hooks from project_root to work_repo
            src_hooks_dir = project_root / ".claude"
            dst_hooks_dir = work_repo / ".claude"
            if src_hooks_dir.exists():
                shutil.copytree(src_hooks_dir, dst_hooks_dir)
            else:
                return False, "No .claude directory found in project root"

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
                    timeout=timeout,
                )

                output = result.stdout + result.stderr

                # Check if the output indicates the command was blocked
                # Claude should report something like "command was blocked" or similar
                blocked_indicators = [
                    "blocked",
                    "not allowed",
                    "prevented",
                    "hook",
                    "refused",
                    "denied",
                    "cannot",
                    "exit code 2",
                ]

                output_lower = output.lower()
                was_blocked = any(ind in output_lower for ind in blocked_indicators)

                if was_blocked:
                    return True, f"Live verification passed: Claude was blocked from running --no-verify\nOutput: {output[:500]}"
                else:
                    return False, f"Live verification FAILED: Claude was NOT blocked\nOutput: {output[:500]}"

            except subprocess.TimeoutExpired:
                return False, f"Live verification timed out after {timeout}s"
            except FileNotFoundError:
                return False, "Claude CLI not found - is it installed?"
            except Exception as e:
                return False, f"Live verification error: {e}"


class CursorAdapter(AiAgentAdapter):
    """Adapter for Cursor IDE."""

    @property
    def agent_type(self) -> AiAgentType:
        return AiAgentType.CURSOR

    def install_hooks(self, project_root: Path) -> list[Path]:
        raise UnsupportedAiAgentError(
            self.agent_type,
            "Cursor support not yet implemented. Use Claude Code for now."
        )

    def verify_hooks(self, project_root: Path) -> VerificationResult:
        raise UnsupportedAiAgentError(
            self.agent_type,
            "Cursor verification not yet implemented."
        )

    def is_installed(self, project_root: Path) -> bool:
        return False


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
    # Normalize: get first word (the executable)
    executable = command.strip().split()[0] if command else ""
    executable = Path(executable).name  # Handle full paths

    # Match patterns
    if re.match(r"^claude", executable, re.IGNORECASE):
        return AiAgentType.CLAUDE_CODE
    elif re.match(r"^cursor", executable, re.IGNORECASE):
        return AiAgentType.CURSOR
    elif re.match(r"^(gh\s+)?copilot", executable, re.IGNORECASE):
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
    elif agent_type == AiAgentType.AIDER:
        return UnsupportedAdapter(agent_type, "Aider has no command hook mechanism")
    elif agent_type == AiAgentType.GEMINI:
        return UnsupportedAdapter(agent_type, "Gemini hooks are in development")
    elif agent_type == AiAgentType.COPILOT:
        return UnsupportedAdapter(agent_type, "Copilot support not yet implemented")
    elif agent_type == AiAgentType.CODEX:
        return UnsupportedAdapter(agent_type, "Codex support not yet implemented")
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

        # Save verification marker if passed
        if result.success:
            marker = VerificationMarker(
                verified_at=datetime.now(),
                meta_agent=agent_type,
                hooks_hash=VerificationMarker.compute_hooks_hash(project_root, agent_type),
                signature="",  # Will be computed
            )
            marker.signature = marker.compute_signature()
            marker.save(project_root)

    return results


def check_verification_status(project_root: Path, config) -> tuple[bool, str]:
    """Check if hooks have been verified and verification is still valid.

    Returns:
        (is_valid, message) tuple
    """
    marker = VerificationMarker.load(project_root)

    if marker is None:
        return False, "Hooks not verified. Run 'issue-orchestrator verify-hooks' first."

    if not marker.is_valid(project_root):
        return False, "Hooks have changed since verification. Re-run 'issue-orchestrator verify-hooks'."

    # Check that marker covers all agents in config
    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    # For now we only support single AI agent type per verification
    # TODO: Support multiple markers for multiple AI agent types
    if marker.meta_agent not in unique_types:
        return False, f"Verification is for {marker.meta_agent.value} but config uses {[t.value for t in unique_types]}"

    return True, f"Hooks verified at {marker.verified_at.isoformat()} for {marker.meta_agent.value}"
