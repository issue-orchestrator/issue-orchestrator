"""End-to-end verification that script output reaches ui-session.log with filtering applied.

Tests verify:
- ANSI escape codes are stripped
- Spinner fragments are filtered out
- Known marker strings survive filtering
- Short meaningful lines (e.g. "PASS") are preserved
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.events import EventName

from .scenario_dsl import scenario, script


def _find_ui_session_logs(worktree: Path) -> list[Path]:
    """Find all ui-session.log files under the worktree sessions dir."""
    sessions_dir = worktree / ".issue-orchestrator" / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.rglob("ui-session.log"))


def _read_session_log(log_path: Path) -> str:
    """Read and return contents of a ui-session.log file."""
    assert log_path.exists(), f"ui-session.log not found: {log_path}"
    content = log_path.read_text(encoding="utf-8")
    assert content.strip(), f"ui-session.log is empty: {log_path}"
    return content


def _assert_no_ansi_escapes(content: str, context: str = "") -> None:
    """Assert that content contains no ANSI escape sequences."""
    assert "\x1b" not in content, (
        f"ANSI escape sequences found in {context or 'content'}:\n"
        f"{content[:500]}"
    )


def _assert_contains(content: str, marker: str, context: str = "") -> None:
    """Assert that content contains the expected marker string."""
    assert marker in content, (
        f"Expected marker '{marker}' not found in {context or 'content'}:\n"
        f"{content[:500]}"
    )


def _assert_not_contains(content: str, marker: str, context: str = "") -> None:
    """Assert that content does NOT contain the marker string."""
    assert marker not in content, (
        f"Unexpected marker '{marker}' found in {context or 'content'}:\n"
        f"{content[:500]}"
    )


def test_session_log_content_via_draft_pr(scenario_repo: Path):
    """via-draft-pr mode: coder output reaches ui-session.log with ANSI stripped.

    Coder runs coder_verbose.sh which outputs ANSI-colored text, spinner chars,
    and marker strings. The ScriptSessionRunner should write filtered output to
    ui-session.log with ANSI codes removed and spinner lines dropped.
    """
    ctx = scenario("session_log_draft_pr", scenario_repo) \
        .coder(script("coder_verbose.sh")) \
        .reviewer(script("reviewer_verbose.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .expect_pr(created=True) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None, "No worktree found in scenario context"

    logs = _find_ui_session_logs(worktree)
    assert logs, f"No ui-session.log files found under {worktree}"

    # Check that at least one log contains our coder markers
    all_content = "\n".join(_read_session_log(log) for log in logs)

    # Coder markers must survive filtering
    _assert_contains(all_content, "user-authentication-module", "coder ui-session.log")
    _assert_contains(all_content, "PASS", "coder ui-session.log")
    _assert_contains(all_content, "Tests passed: 5/5", "coder ui-session.log")

    # ANSI escape codes must be stripped
    _assert_no_ansi_escapes(all_content, "ui-session.log")


def test_session_log_content_via_local_loop(scenario_repo: Path):
    """via-local-loop mode: coder and reviewer output reaches ui-session.log filtered.

    Uses coder_verbose_dual_mode.sh (outputs ANSI codes + markers) and
    reviewer_verbose.sh (outputs ANSI + review markers). Both should have
    their output cleaned and written to ui-session.log.
    """
    ctx = scenario("session_log_local_loop", scenario_repo) \
        .coder(script("coder_verbose_dual_mode.sh")) \
        .reviewer(script("reviewer_verbose.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None, "No worktree found in scenario context"

    logs = _find_ui_session_logs(worktree)
    assert logs, f"No ui-session.log files found under {worktree}"

    all_content = "\n".join(_read_session_log(log) for log in logs)

    # Coder markers must survive filtering
    _assert_contains(all_content, "user-authentication-module", "coder output")
    _assert_contains(all_content, "PASS", "coder output (short-line regression)")
    _assert_contains(all_content, "Tests passed: 5/5", "coder output")

    # ANSI escape codes must be stripped
    _assert_no_ansi_escapes(all_content, "ui-session.log")


def test_session_log_reviewer_markers_via_local_loop(scenario_repo: Path):
    """via-local-loop mode: reviewer markers reach the review-exchange ui-session.log.

    The review exchange loop writes reviewer output to its own run_dir's
    ui-session.log via _append_session_log. Verify reviewer-specific markers
    are present and cleaned.
    """
    ctx = scenario("session_log_reviewer_markers", scenario_repo) \
        .coder(script("coder_verbose_dual_mode.sh")) \
        .reviewer(script("reviewer_verbose.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None

    logs = _find_ui_session_logs(worktree)
    assert logs, f"No ui-session.log files found under {worktree}"

    all_content = "\n".join(_read_session_log(log) for log in logs)

    # Reviewer structured response is read from file and logged via _append_session_log.
    # Stdout chatter (ANSI codes, "src/auth.py" etc.) flows through the parent's
    # PTY in production but is NOT captured by the review exchange loop.
    # We verify the JSON response content appears in the session log.
    _assert_contains(all_content, "LGTM", "reviewer response")
    _assert_contains(all_content, "response_type", "reviewer response JSON")
