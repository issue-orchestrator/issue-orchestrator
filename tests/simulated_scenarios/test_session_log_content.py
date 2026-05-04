"""End-to-end verification that script output reaches the canonical session artifact.

Tests verify:
- raw terminal recordings are created for PTY-backed runs
- known marker strings survive raw capture
- ANSI output is preserved losslessly for emulator replay
- review-exchange transcript content remains available

NOTE: the via-local-loop variants of these tests are skipped after the
persistent-session cutover. The persistent runner manages its own
subprocesses via PTY directly rather than going through the
``ScriptSessionRunner`` port the scenario harness injects. The
persistent runner's capture invariant is covered by
``tests/unit/execution/test_persistent_session_exchange.py``
(``test_recording_path_captures_continuous_log_across_rounds``).
Migrating the simulated-scenario harness to drive the persistent
runner is tracked as a follow-up.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from issue_orchestrator.events import EventName

from .conftest import ScriptSessionRunner
from .scenario_dsl import scenario, script


def _find_session_artifacts(worktree: Path) -> list[Path]:
    """Find session text/recording artifacts under the worktree sessions dir."""
    sessions_dir = worktree / ".issue-orchestrator" / "sessions"
    if not sessions_dir.exists():
        return []
    artifacts = list(sessions_dir.rglob("terminal-recording.jsonl"))
    artifacts.extend(sessions_dir.rglob("ui-session.log"))
    artifacts.extend(sessions_dir.rglob("review-exchange/transcript.log"))
    return sorted({path for path in artifacts if path.exists() and path.stat().st_size > 0})


def _read_session_artifact(log_path: Path) -> str:
    """Read and return decoded session artifact content."""
    assert log_path.exists(), f"session artifact not found: {log_path}"
    if log_path.name == "terminal-recording.jsonl":
        content = _decode_terminal_recording(log_path)
    else:
        content = log_path.read_text(encoding="utf-8")
    assert content.strip(), f"session artifact is empty: {log_path}"
    return content


def _decode_terminal_recording(path: Path) -> str:
    chunks: list[str] = []
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw_line in raw_lines:
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            chunks.append(raw_line)
            continue
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(chunks)


def _assert_contains_ansi_escapes(content: str, context: str = "") -> None:
    """Assert that raw content preserves ANSI escape sequences."""
    assert "\x1b" in content, (
        f"Expected ANSI escape sequences in {context or 'content'}:\n"
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
    """via-draft-pr mode: coder output reaches terminal recording losslessly.

    Coder runs coder_verbose.sh which outputs ANSI-colored text, spinner chars,
    and marker strings. The PTY-backed runner should record that output raw so
    replay tooling can render it later.
    """
    ctx = scenario("session_log_draft_pr", scenario_repo) \
        .use_runner(ScriptSessionRunner()) \
        .coder(script("coder_verbose.sh")) \
        .reviewer(script("reviewer_verbose.sh")) \
        .review_exchange(mode="via-draft-pr") \
        .expect_pr(created=True) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None, "No worktree found in scenario context"

    logs = _find_session_artifacts(worktree)
    assert logs, f"No session artifacts found under {worktree}"

    all_content = "\n".join(_read_session_artifact(log) for log in logs)

    _assert_contains(all_content, "user-authentication-module", "coder session artifact")
    _assert_contains(all_content, "PASS", "coder session artifact")
    _assert_contains(all_content, "Tests passed: 5/5", "coder session artifact")

    _assert_contains_ansi_escapes(all_content, "terminal recording")


@pytest.mark.skip(
    reason="Persistent-session cutover bypasses ScriptSessionRunner; "
    "capture invariant covered by test_persistent_session_exchange.py "
    "(test_recording_path_captures_continuous_log_across_rounds)."
)
def test_session_log_content_via_local_loop(scenario_repo: Path):
    """via-local-loop mode: coder and reviewer output reaches session artifacts.

    Uses coder_verbose_dual_mode.sh (outputs ANSI codes + markers) and
    reviewer_verbose.sh (outputs ANSI + review markers). Both should have
    their output captured in the canonical session artifacts.
    """
    ctx = scenario("session_log_local_loop", scenario_repo) \
        .use_runner(ScriptSessionRunner()) \
        .coder(script("coder_verbose_dual_mode.sh")) \
        .reviewer(script("reviewer_verbose.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None, "No worktree found in scenario context"

    logs = _find_session_artifacts(worktree)
    assert logs, f"No session artifacts found under {worktree}"

    all_content = "\n".join(_read_session_artifact(log) for log in logs)

    # Coder markers must survive filtering
    _assert_contains(all_content, "user-authentication-module", "coder output")
    _assert_contains(all_content, "PASS", "coder output (short-line regression)")
    _assert_contains(all_content, "Tests passed: 5/5", "coder output")

    _assert_contains_ansi_escapes(all_content, "session artifacts")


@pytest.mark.skip(
    reason="Persistent-session cutover deleted the spawn-per-phase "
    "capture path these scenarios were tightly coupled to. The "
    "persistent runner is exhaustively unit-tested in "
    "test_persistent_session_exchange.py + test_persistent_round_runner.py; "
    "migrating this harness to drive the persistent runner natively "
    "is tracked as a follow-up."
)
def test_session_log_reviewer_markers_via_local_loop(scenario_repo: Path):
    """via-local-loop mode: reviewer markers reach the review-exchange artifacts.

    The review exchange loop records reviewer output in its run-scoped artifacts.
    Verify reviewer-specific markers are present.
    """
    ctx = scenario("session_log_reviewer_markers", scenario_repo) \
        .use_runner(ScriptSessionRunner()) \
        .coder(script("coder_verbose_dual_mode.sh")) \
        .reviewer(script("reviewer_verbose.sh", prompt=True)) \
        .review_exchange(mode="via-local-loop", require_validation=False) \
        .expect_event(EventName.REVIEW_EXCHANGE_COMPLETED) \
        .run()

    worktree = ctx.worktree
    assert worktree is not None

    logs = _find_session_artifacts(worktree)
    assert logs, f"No session artifacts found under {worktree}"

    all_content = "\n".join(_read_session_artifact(log) for log in logs)

    # Reviewer structured response is preserved in the dedicated review-exchange
    # transcript instead of polluting the canonical terminal replay.
    _assert_contains(all_content, "LGTM", "reviewer response")
    _assert_contains(all_content, "response_type", "reviewer response JSON")
