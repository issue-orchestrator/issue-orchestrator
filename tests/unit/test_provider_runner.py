"""Tests for provider_runner.

The key architectural invariant: provider_runner does NOT capture agent
output. Output flows through the parent's PTY (pexpect) to CleaningLogWriter.
provider_runner only handles retry/circuit-breaker status reporting.
"""

from issue_orchestrator.entrypoints.cli_tools.provider_runner import _summarize_error


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
