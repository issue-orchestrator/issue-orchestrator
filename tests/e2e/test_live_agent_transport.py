"""Live agent transport acceptance checks.

These tests are e2e-managed because they run real provider CLIs and can fail
for non-code reasons such as local auth, provider availability, or network
behavior. Keep deterministic transport-contract coverage in unit/integration
tests; this module proves the live TUI contract still holds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.persistent_round_runner import (
    close_persistent_session,
    open_persistent_session,
    send_round,
)
from tests.e2e.fixtures import get_test_repo, is_gh_authenticated, is_github_reachable


def _claude_authenticated() -> bool:
    """Return whether the local Claude CLI can run a minimal prompt."""
    if shutil.which("claude") is None:
        return False
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "Reply with OK"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


_CLAUDE_READY = _claude_authenticated()

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
    pytest.mark.skipif(not is_gh_authenticated(), reason="GitHub CLI not authenticated"),
    pytest.mark.skipif(
        not is_github_reachable(get_test_repo()),
        reason="GitHub API not reachable",
    ),
    pytest.mark.skipif(
        not _CLAUDE_READY,
        reason="Claude CLI not installed or not authenticated",
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
