"""Live integration test for real coding + real review cycle.

This test intentionally exercises a production-like path:
1. Real Claude coding run with the exact #4057 prompt.
2. Real reviewer/coder exchange loop.
3. Assertions on stage artifacts so regressions are test-detected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from issue_orchestrator.control.review_exchange_loop import run_review_exchange_loop
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin
from issue_orchestrator.infra.env import ENV_PREFIX

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.requires_infra,
    pytest.mark.xdist_group("pty"),
    pytest.mark.timeout(35 * 60),
]

ISSUE_4057_PROMPT = """IMPORTANT: This worktree has 54 existing commit(s) from a previous session. Branch: 4057-ui-surface-provider-circuit-breaker-status. Commits:   - 3592261 fix: Improve session log symlink handling in provider_runner
  - 7d3af45 fix: Mark agent-done integration tests as xfail due to provider_runner issue
  - d088aab fix: Mark foreign repo lifecycle tests as xfail due to provider_runner integration issue
  - 6464759 chore: Update session tracking for issue #4057 worktree completion
  - 39cd869 chore: Update session tracking
  - 96cd3a6 fix: Add missing mock return values for tracking branch setup in worktree tests
  - 5004703 chore: Update session tracking after evaluation completion
  - 99fcf51 chore: Update session tracking after validation completion
  - 79567ab chore: Update session tracking after validation completion
  - 710e19c chore: Update session tracking after final validation
  ... and 44 more. EVALUATE this existing work BEFORE starting fresh.

Work on issue #4057: UI: Surface provider circuit breaker status. Follow the instructions in repo-specific/prompts/simple-fix.md. When done, exit with /exit."""


def _claude_available() -> bool:
    return shutil.which("claude") is not None


def _wait_until(condition, *, timeout_seconds: int, interval_seconds: float = 0.2) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval_seconds)
    return False


@pytest.mark.skipif(not _claude_available(), reason="Claude CLI not installed")
def test_real_claude_coding_and_review_cycle_uses_4057_prompt(tmp_path, monkeypatch):
    source_repo_root = Path(__file__).resolve().parents[2]
    worktree = tmp_path / f"real-worktree-{uuid4().hex[:8]}"
    subprocess.run(
        [
            "git", "-C", str(source_repo_root), "worktree", "add",
            "--detach", str(worktree), "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        (worktree / ".issue-orchestrator").mkdir(exist_ok=True)
        plugin_state_root = tmp_path / "plugin-state-root"
        plugin_state_root.mkdir()
        monkeypatch.setenv(f"{ENV_PREFIX}REPO_ROOT", str(plugin_state_root))

        session_output = FileSystemSessionOutput()
        coding_session_name = "coding-4057-live"
        coding_run = session_output.start_run(
            worktree,
            coding_session_name,
            issue_number=4057,
            agent_label="agent:backend",
            backend="subprocess",
        )
        completion_path = coding_run.run_dir / "completion-record.json"
        completion_rel = completion_path.relative_to(worktree)
        escaped_prompt = ISSUE_4057_PROMPT.replace('"', '\\"')
        claude_cmd = (
            f"export {ENV_PREFIX}COMPLETION_PATH=\"{completion_rel}\""
            f" && export {ENV_PREFIX}AGENT_LABEL=\"agent:backend\""
            f" && export {ENV_PREFIX}ISSUE_NUMBER=\"4057\""
            f" && claude --print --dangerously-skip-permissions \"{escaped_prompt}\""
        )

        plugin = SubprocessPlugin()
        created = plugin.create_session(
            session_id=4057,
            command=claude_cmd,
            working_dir=str(worktree),
            title="Live coding #4057",
            session_name=coding_session_name,
        )
        assert created is True

        exited = _wait_until(
            lambda: not bool(plugin.session_exists(0, coding_session_name)),
            timeout_seconds=25 * 60,
        )
        assert exited, "Coding session did not exit within 25 minutes"
        coding_log = coding_run.run_dir / "ui-session.log"
        assert coding_log.exists(), "Missing coding ui-session.log"
        assert coding_log.stat().st_size > 0, "Coding ui-session.log is empty"
        assert completion_path.exists(), (
            "Coding completion record missing. "
            f"ui-session.log:\n{coding_log.read_text(errors='replace')[:4000]}"
        )

        prompt_stub = worktree / "review-prompt-stub.md"
        prompt_stub.write_text("review-exchange prompt stub\n", encoding="utf-8")
        coder_agent = AgentConfig(
            prompt_path=prompt_stub,
            ai_system="claude-code",
            permission_mode="bypassPermissions",
            timeout_minutes=25,
        )
        reviewer_agent = AgentConfig(
            prompt_path=prompt_stub,
            ai_system="claude-code",
            permission_mode="bypassPermissions",
            timeout_minutes=25,
        )

        review_outcome = run_review_exchange_loop(
            session_output=session_output,
            worktree_path=worktree,
            issue_number=4057,
            issue_title="UI: Surface provider circuit breaker status",
            coder_label="agent:backend",
            reviewer_label="agent:reviewer",
            coder_agent=coder_agent,
            reviewer_agent=reviewer_agent,
            max_rounds=3,
            max_no_progress=2,
            require_validation=True,
            web_port=None,
        )

        assert review_outcome.exchange_dir is not None
        review_run_dir = review_outcome.exchange_dir.parent
        review_log = review_run_dir / "ui-session.log"
        assert review_log.exists(), "Missing review ui-session.log"
        assert review_log.stat().st_size > 0, "Review ui-session.log is empty"
        assert (review_run_dir / "completion-coder.json").exists(), "Missing completion-coder.json"
        assert (review_run_dir / "validation-record.json").exists(), "Missing validation-record.json"
        summary_path = review_run_dir / "review-exchange" / "summary.json"
        assert summary_path.exists(), "Missing review-exchange summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert isinstance(summary, dict)
        assert "status" in summary
    finally:
        keep_worktree = os.environ.get("KEEP_REAL_WORKTREE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if keep_worktree:
            print(f"[real-cycle] preserving worktree for audit: {worktree}")
        else:
            subprocess.run(
                ["git", "-C", str(source_repo_root), "worktree", "remove", "--force", str(worktree)],
                check=False,
                capture_output=True,
                text=True,
            )
