"""Lightweight probes for live agent CLI availability in tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import cache


def is_claude_available() -> bool:
    """Return whether the Claude CLI is available in PATH."""
    return shutil.which("claude") is not None


@cache
def is_claude_authenticated() -> bool:
    """Return whether the Claude CLI can run a minimal prompt.

    This is a live provider probe, so call it from live-agent test bodies or
    live-agent lanes, not broad e2e collection. Scrub ``CLAUDECODE`` so the
    probe works when tests run inside a Claude Code session.
    """
    if not is_claude_available():
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
