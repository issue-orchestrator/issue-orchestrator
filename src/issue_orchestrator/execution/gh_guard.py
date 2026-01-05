"""Guard against direct gh CLI usage.

This blocks subprocess calls that invoke `gh` directly. All GitHub access
should go through the GitHub HTTP client/adapter to ensure auditing,
rate-limit discipline, and consistent retries.
"""

from __future__ import annotations

import os
import subprocess

_GH_GUARD_ENABLED = os.environ.get("ORCHESTRATOR_GH_GUARD", "1") != "0"
_ORIGINAL_SUBPROCESS_RUN = subprocess.run
_GH_GUARD_INSTALLED = False


def install_gh_guard() -> None:
    """Prevent direct gh CLI invocations across the process."""
    global _GH_GUARD_INSTALLED
    if _GH_GUARD_INSTALLED or not _GH_GUARD_ENABLED:
        return

    def _guarded_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "gh":
            raise RuntimeError(
                "Direct gh invocation is forbidden; use GitHubHttpClient/GitHubAdapter"
            )
        return _ORIGINAL_SUBPROCESS_RUN(*args, **kwargs)

    subprocess.run = _guarded_run
    _GH_GUARD_INSTALLED = True
