"""Cache-busting version token + sidebar commit-SHA resolution.

The Control Center serves ``/static/*`` with browser-default caching,
so when an operator upgrades the cc and reopens the dashboard the
browser keeps serving the previous JS/CSS until they hard-reload. The
PR #6263 shutdown-reason rollout hit this exact trap: a freshly
upgraded cc enforced the new ``reason`` requirement, but the browser
was still running the pre-merge ``control_center.js`` that didn't send
``reason`` — and the Stop-engine button silently 400'd.

Two surfaces here:

- ``STATIC_VERSION_TOKEN`` — appended as ``?v=<token>`` to JS/CSS
  references in ``control_center.html``. Computed once at import
  time so every cc process restart automatically invalidates the
  browser cache.
- ``resolve_cc_commit_sha`` — what the sidebar shows. Walks up
  from the package install location (not ``Path.cwd()``) so a cc
  launched from outside its source checkout still reports a useful
  SHA instead of "unknown".
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .repo_identity import _resolve_git_dir, get_repo_head_sha


def _walk_up_for_git_dir(start: Path) -> Optional[Path]:
    """Walk up parents from ``start`` looking for a directory that contains a git dir.

    ``_resolve_git_dir`` only checks the directory it is given, so a
    package installed at ``…/src/issue_orchestrator/`` would never see
    the worktree root's ``.git``. This helper closes the gap.
    """
    current = start.resolve()
    for candidate in (current, *current.parents):
        if _resolve_git_dir(candidate) is not None:
            return candidate
    return None


_PACKAGE_DIR = Path(__file__).resolve().parent.parent  # …/src/issue_orchestrator
_PACKAGE_REPO_ROOT = _walk_up_for_git_dir(_PACKAGE_DIR)


def resolve_cc_commit_sha() -> Optional[str]:
    """Return the cc's source commit SHA, or ``None`` for non-source installs.

    Prefers the package-relative repo root (where the running code
    actually lives) over ``Path.cwd()``, which is whatever directory
    the operator ran the launcher from and is rarely a useful repo.
    """
    if _PACKAGE_REPO_ROOT is None:
        return None
    return get_repo_head_sha(_PACKAGE_REPO_ROOT)


def _compute_static_version_token() -> str:
    """Pick the most stable identifier available at import time.

    Source installs fingerprint by commit SHA — switching branches and
    restarting the cc gives a new token. Wheel installs and other
    non-source layouts fall back to the process start time, which still
    invalidates on every cc restart (the only event that matters for
    the browser-cache trap).
    """
    sha = resolve_cc_commit_sha()
    if sha:
        return sha[:12]
    # Process-startup epoch as fallback. Restart = new token, which is
    # exactly when we need to invalidate.
    return f"start-{int(time.time())}"


STATIC_VERSION_TOKEN: str = _compute_static_version_token()


__all__ = [
    "STATIC_VERSION_TOKEN",
    "resolve_cc_commit_sha",
]
