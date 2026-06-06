"""Live agent transport acceptance checks.

These tests are e2e-managed because they run real provider CLIs and can fail
for non-code reasons such as local auth, provider availability, or network
behavior. Keep deterministic transport-contract coverage in unit/integration
tests; this module proves the live TUI contract still holds.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.persistent_round_runner import (
    close_persistent_session,
    open_persistent_session,
    send_round,
)
from tests.fixtures.live_agent_cli import (
    is_claude_authenticated,
    is_claude_available,
)

# Only cheap checks may run at collection time: this module is imported by
# every `pytest tests/e2e` invocation (including test-e2e-one and
# --collect-only), so the live auth probe is deferred into the test body via
# is_claude_authenticated(). No GitHub gating: this test drives a local
# Claude PTY and never touches the GitHub API (test_gh_activity_limit=0).
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.live_agent,
    pytest.mark.heavy_e2e,
    pytest.mark.requires_infra,
    pytest.mark.xdist_group("live-agent"),
    pytest.mark.gh_activity_limit(
        test_gh_activity_limit=0,
        system_gh_activity_limit=20,
    ),
    pytest.mark.skipif(
        not is_claude_available(),
        reason="Claude CLI not installed",
    ),
]


def _venv_path_prefix() -> str:
    venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
    return f"{venv_bin}:{os.environ.get('PATH', '')}"


def test_persistent_send_round_two_rounds_real_claude() -> None:
    """A persistent real Claude TUI accepts two CR-submitted rounds.

    This is the review-exchange path: a long-lived interactive Claude process
    driven across rounds by injecting prompts on its PTY. The tixmeup
    #277/#290 hang lived here: ``send_round`` used to terminate prompts with
    ``"\n"``, which a raw-mode TUI does not treat as Enter, so the prompt
    rendered into the input box but was never submitted. This proves the
    ``"\r"`` fix against real Claude across two rounds.
    """
    if not is_claude_authenticated():
        pytest.skip("Claude CLI not authenticated")

    # Run under the repo worktree (a trusted git repo) so the interactive
    # first-run "trust this folder?" dialog never blocks — mirrors
    # production, where review-exchange worktrees are trusted. /tmp would
    # trigger that dialog for an interactive session.
    repo_root = Path(__file__).resolve().parents[2]
    work_dir = Path(tempfile.mkdtemp(prefix=".live-send-round-", dir=repo_root))
    response_file = work_dir / "review-response.json"
    system_prompt = (
        "You are an automated review-exchange test agent. Every time you "
        "receive a message, immediately use the Bash tool to run exactly:\n"
        "  printf "
        '\'{"response_type":"ok"}\' > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"\n'
        "then keep waiting for the next message. Do not exit on your own."
    )
    # Scrub CLAUDECODE so the nested-session guard does not block claude
    # when this test runs inside a Claude Code session (same scrub as the
    # is_claude_authenticated probe).
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["PATH"] = _venv_path_prefix()
    env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(response_file)

    session = open_persistent_session(
        command=[
            "claude",
            "--model",
            "haiku",
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt",
            system_prompt,
        ],
        working_dir=work_dir,
        env=env,
    )
    try:
        from issue_orchestrator.execution.persistent_round_runner import (
            _drain_pty_output,
        )

        # Let claude boot to its idle input prompt (trusted dir -> no dialog).
        boot_deadline = time.monotonic() + 25
        while time.monotonic() < boot_deadline:
            _drain_pty_output(session)
            if session.proc.poll() is not None:
                pytest.fail(f"claude exited during boot, code={session.proc.poll()}")
            time.sleep(0.3)

        for n in (1, 2):
            response_file.unlink(missing_ok=True)
            result = send_round(
                session,
                prompt=f"round {n}: respond now",
                response_file=response_file,
                timeout_seconds=60,
                poll_interval_seconds=0.3,
                role_label=f"coder@round-{n}",
            )
            assert result == {"response_type": "ok"}, (
                f"round {n} not answered - a CR-terminated prompt must submit "
                f"to real claude. Got: {result}"
            )
            assert session.proc.poll() is None, f"claude exited after round {n}"
    finally:
        close_persistent_session(session)
        shutil.rmtree(work_dir, ignore_errors=True)
