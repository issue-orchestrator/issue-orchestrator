"""Tests for provider_runner.

The key architectural invariant: provider_runner does NOT capture agent
output. Output flows through the parent's PTY (pexpect) to CleaningLogWriter.
provider_runner only handles retry/circuit-breaker status reporting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from issue_orchestrator.entrypoints.cli_tools.provider_runner import (
    _build_command,
    _summarize_error,
)

if TYPE_CHECKING:
    import pytest


def test_summarize_error_returns_none_for_empty() -> None:
    assert _summarize_error("") is None
    assert _summarize_error("   ") is None


def test_summarize_error_returns_short_text() -> None:
    assert _summarize_error("rate limit exceeded") == "rate limit exceeded"


def test_summarize_error_truncates_long_text() -> None:
    long_text = "x" * 500
    result = _summarize_error(long_text)
    assert result is not None
    assert len(result) == 303  # 300 + "..."
    assert result.endswith("...")


# ---------------------------------------------------------------------------
# _build_command tests
# ---------------------------------------------------------------------------


def test_build_command_uses_sh_not_user_shell(monkeypatch: "pytest.MonkeyPatch") -> None:
    """_build_command must use /bin/sh, not $SHELL.

    zsh resets inherited SIG_IGN for SIGTTIN/SIGTTOU (violating POSIX),
    which causes agents to freeze when they open /dev/tty from a background
    process group. Using /bin/sh avoids this.
    """
    monkeypatch.setenv("SHELL", "/bin/zsh")
    result = _build_command("claude --model haiku")
    assert result[0] == "/bin/sh", f"Expected /bin/sh, got {result[0]} — $SHELL must not be used"
    assert result[1] == "-c"
    assert result[2] == "claude --model haiku"


def test_build_command_ignores_shell_env(monkeypatch: "pytest.MonkeyPatch") -> None:
    """$SHELL must be completely ignored, even if set to an exotic shell."""
    monkeypatch.setenv("SHELL", "/usr/local/bin/fish")
    result = _build_command("echo hello")
    assert result[0] == "/bin/sh"


def test_build_command_no_login_flag() -> None:
    """No login flag (-l) needed; outer SubprocessPlugin shell sets up env."""
    result = _build_command("echo hello")
    assert "-l" not in result[1], "Login flag is unnecessary — env is inherited"
    assert "-lc" not in result[1], "Login flag is unnecessary — env is inherited"
