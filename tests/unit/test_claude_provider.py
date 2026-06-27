"""Unit tests for ``ClaudeCodeProvider.build_command``."""

from __future__ import annotations

import pytest

from issue_orchestrator.execution.agent_runner_providers.claude import ClaudeCodeProvider


def _cmd(**kwargs: str) -> list[str]:
    return ClaudeCodeProvider().build_command(prompt="task", **kwargs)


def test_effort_passes_claude_effort_flag() -> None:
    cmd = _cmd(model="opus", effort="xhigh")

    assert cmd[:5] == ["claude", "--model", "opus", "--effort", "xhigh"]


def test_reasoning_effort_alias_passes_claude_effort_flag() -> None:
    cmd = _cmd(reasoning_effort="XHIGH")

    effort_idx = cmd.index("--effort")
    assert cmd[effort_idx + 1] == "xhigh"


def test_matching_effort_aliases_normalize_case() -> None:
    cmd = _cmd(effort="xhigh", reasoning_effort="XHIGH")

    effort_idx = cmd.index("--effort")
    assert cmd[effort_idx + 1] == "xhigh"


def test_conflicting_effort_aliases_fail_fast() -> None:
    with pytest.raises(ValueError, match="effort and reasoning_effort must match"):
        _cmd(effort="high", reasoning_effort="xhigh")


def test_invalid_effort_fails_fast() -> None:
    with pytest.raises(ValueError, match="Claude effort must be one of"):
        _cmd(effort="ultra")
