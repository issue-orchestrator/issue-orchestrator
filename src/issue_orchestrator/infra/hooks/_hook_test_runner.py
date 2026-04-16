"""Shared hook-script verification helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

HookInputFormat = Literal["tool_input_command", "command", "copilot_tool_args"]
HookBlockMode = Literal["exit_code_2", "cursor_permission", "copilot_permission"]
ReturnStream = Literal["stderr", "stdout"] | None
HookBlockTester = Callable[[Path, str], bool | tuple[bool, str]]

HOOK_TEST_CASES: tuple[tuple[str, bool], ...] = (
    ("git push --no-verify", True),
    ("git commit --no-verify -m 'test'", True),
    ("git push origin main --no-verify", True),
    ("git --no-verify push", True),
    ("git commit -n -m 'test'", True),
    ("git -c core.hooksPath=/dev/null push", True),
    ("git config --local core.hooksPath /dev/null", True),
    ("gh pr merge 123", True),
    ("gh pr merge 123 --squash", True),
    ("gh api repos/owner/repo/pulls/123/merge -X PUT", True),
    ("git push origin main", False),
    ("git commit -m 'test'", False),
    ("gh pr create --title 'test'", False),
    ("gh pr view 123", False),
    ("ls -la", False),
)


def run_hook_test_cases(
    blocks_hook: HookBlockTester,
    hook_script: Path,
    checks_passed: list[str],
    checks_failed: list[str],
) -> None:
    """Run the shared guardrail hook matrix and record results."""
    for cmd, should_block in HOOK_TEST_CASES:
        result = blocks_hook(hook_script, cmd)
        blocked = result[0] if isinstance(result, tuple) else result
        label = cmd[:30]
        if should_block == blocked:
            checks_passed.append(f"{'blocks' if should_block else 'allows'}:{label}")
        else:
            checks_failed.append(f"{'should_block' if should_block else 'wrongly_blocks'}:{label}")


def build_hook_input(command: str, input_format: HookInputFormat) -> str:
    """Build one agent's hook input envelope for a shell command."""
    if input_format == "tool_input_command":
        return json.dumps({"tool_input": {"command": command}})
    if input_format == "command":
        return json.dumps({"command": command})
    return json.dumps({"toolName": "bash", "toolArgs": json.dumps({"command": command})})


def is_blocked(*, returncode: int, stdout: str, block_mode: HookBlockMode) -> bool:
    """Parse one hook execution result into a blocked/allowed decision."""
    if block_mode == "exit_code_2":
        return returncode == 2
    if block_mode == "cursor_permission":
        return _json_stdout(stdout).get("permission") == "deny"
    return _json_stdout(stdout).get("permissionDecision") == "deny"


def _json_stdout(stdout: str) -> dict[str, object]:
    try:
        return json.loads(stdout.strip()) if stdout.strip() else {}
    except json.JSONDecodeError:
        return {}
