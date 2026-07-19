"""OS-level proof that the ADR-0034 claude-code sandbox settings hold.

This is the integration counterpart to the pure-JSON unit tests in
``tests/unit/test_sandbox_provider_adapter.py``. Those assert the *shape* of the
``--settings`` payload; this test launches a **real** ``claude`` non-interactively
with the **generated** sandbox argv in a temp worktree and proves the operating
system actually enforces the boundary, by running one Bash command through the
agent and inspecting its side effects on disk.

What it proves (all via deterministic on-disk side effects, not model prose):

1. **Positive control** — a write *inside* the worktree succeeds, which proves
   the agent really executed sandboxed Bash (so the negatives below are real
   denials, not "the agent never ran the command").
2. **Write escape denied** — a write to a path *outside* the worktree never
   lands on disk (writes are cwd-bounded).
3. **Secret read denied (fail-closed layer)** — a secret planted at a
   ``credentials.files``-denied path is unreadable. The secret lives OUTSIDE the
   home dir (``/var/folders`` or ``/tmp``), where the default read policy is
   OPEN and ``denyRead: ["~/"]`` does NOT reach — so this isolates and proves the
   ``credentials.files`` deny, which is the direct fix for the review's
   ``~/.ssh`` / api-token concern.
4. **Network egress denied** — a raw TCP connect to a non-allowed host does not
   open (only ``api.anthropic.com`` is pre-allowed for Bash under model-only).

Skip conditions:
- ``claude`` is not on PATH (mirrors the other live-agent gating).
- native Windows (the sandbox is macOS/Linux/WSL2 only).
- the sandbox cannot initialize on this host (e.g. bubblewrap missing on Linux):
  with ``failIfUnavailable: true`` claude fails to start, which we detect via
  the positive-control write and skip rather than report a false failure.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.domain.sandbox_scope import (
    SandboxScopeContext,
    compute_session_scope,
)
from issue_orchestrator.execution.agent_runner_providers.sandbox import (
    build_claude_sandbox_argv,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.live,
    pytest.mark.live_agent,
]

# Substrings in claude's output that mean the sandbox could not initialize on
# this host (as opposed to a genuine boundary failure) — used to skip, not fail.
_SANDBOX_UNAVAILABLE_SIGNS = (
    "bubblewrap",
    "sandbox is unavailable",
    "sandbox unavailable",
    "failed to start sandbox",
    "could not start sandbox",
)


def _claude_available() -> bool:
    return shutil.which("claude") is not None


def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # claude -p returns empty output when nested inside a running Claude Code
    # session; match production, which starts outside one.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        start_new_session=True,
    )


@pytest.mark.skipif(not _claude_available(), reason="claude CLI not installed")
@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="claude sandbox is unsupported on native Windows (needs WSL2)",
)
def test_generated_sandbox_settings_enforced_by_os(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)

    # A secret OUTSIDE both the worktree and the home dir: the default read
    # policy is OPEN here, so only credentials.files can lock it down.
    secret = tmp_path / "planted-secret.txt"
    secret_marker = "TOPSECRET_MARKER_7f3a"
    secret.write_text(secret_marker + "\n", encoding="utf-8")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    # Result sinks (inside the worktree, the only writable root).
    inside = worktree / "inside.txt"
    escaped = outside_dir / "escaped.txt"
    secret_read = worktree / "secret_read.txt"
    net_out = worktree / "net_out.txt"

    # Production scope for a coder session, plus the planted secret added to the
    # fail-closed deny list so we exercise the real credentials.files path.
    agent = AgentConfig(
        prompt_path=Path(".prompts/backend.md"),
        prompt_relative=".prompts/backend.md",
        provider="claude-code",
        sandbox=True,
    )
    scope = compute_session_scope(
        agent, SandboxScopeContext(task_kind="code", worktree=worktree)
    )
    assert scope is not None
    scope = replace(scope, deny_read_files=scope.deny_read_files + (str(secret),))
    sandbox_argv = build_claude_sandbox_argv(scope)

    # Four SEPARATE Bash commands. This matters: the claude-code sandbox
    # auto-allows filesystem-only commands (they run sandboxed and the OS
    # enforces the boundary) but a command that needs the network is not
    # sandboxable and falls back to the permission flow. Compounding a network
    # probe with the filesystem probes would taint the whole invocation into the
    # permission path and none would run — so each probe is its own command and
    # writes its own result file, which we inspect.
    probes = "\n".join(
        [
            f"1) echo INSIDE_OK > {inside}",
            f"2) echo ESCAPED > {escaped}",
            f"3) cat {secret} > {secret_read} 2>&1",
            f"4) bash -c 'exec 3<>/dev/tcp/example.com/80 && echo OPENED || "
            f"echo CLOSED' > {net_out} 2>&1",
        ]
    )
    prompt = (
        "You are in an automated sandbox test. Use the Bash tool to run EACH of "
        "these as a SEPARATE command (four separate Bash tool calls), then reply "
        f"DONE:\n{probes}\n"
    )
    cmd = ["claude", "--print", "--model", "haiku", *sandbox_argv, prompt]

    result = _run(cmd, cwd=worktree, timeout=180)
    combined = (result.stdout or "") + (result.stderr or "")

    # If the sandbox could not initialize on this host, failIfUnavailable makes
    # claude bail — detect and skip instead of reporting a false failure.
    if not inside.exists():
        lowered = combined.lower()
        if any(sign in lowered for sign in _SANDBOX_UNAVAILABLE_SIGNS):
            pytest.skip(f"sandbox unavailable on this host: {combined[:400]}")
        if "invalid api key" in lowered or "please run /login" in lowered:
            pytest.skip("claude is not authenticated on this host")

    # 1. Positive control: sandboxed Bash actually ran and wrote inside the wt.
    assert inside.exists(), (
        "positive-control write did not land — claude did not run the sandboxed "
        f"Bash command.\nrc={result.returncode}\noutput:\n{combined[:1000]}"
    )
    assert inside.read_text(encoding="utf-8").strip() == "INSIDE_OK"

    # 2. Write escape denied: the outside path must never appear on disk.
    assert not escaped.exists(), (
        "SANDBOX BREACH: a write outside the worktree succeeded "
        f"({escaped}). output:\n{combined[:1000]}"
    )

    # 3. Secret read denied (credentials.files fail-closed layer). The redirect
    #    creates secret_read.txt even when cat is denied, so its presence proves
    #    the command ran; the marker must NOT have been read.
    assert secret_read.exists(), "secret-read probe did not run"
    assert secret_marker not in secret_read.read_text(encoding="utf-8"), (
        "SANDBOX BREACH: a credentials.files-denied secret was read.\n"
        f"contents:\n{secret_read.read_text(encoding='utf-8')[:500]}"
    )

    # 4. Network egress denied: the raw connect to a non-allowed host must fail.
    assert net_out.exists(), "network probe did not run"
    assert "OPENED" not in net_out.read_text(encoding="utf-8"), (
        "SANDBOX BREACH: a TCP connection to a non-allowed host opened.\n"
        f"contents:\n{net_out.read_text(encoding='utf-8')[:500]}"
    )
