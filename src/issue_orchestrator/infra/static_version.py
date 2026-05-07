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

import os
import re
import time
from pathlib import Path
from typing import Optional

from .repo_identity import _resolve_git_dir, get_repo_head_sha

_CC_COMMIT_SHA_ENV = "ISSUE_ORCHESTRATOR_CC_COMMIT_SHA"
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _commit_sha_from_env() -> str | None:
    raw = os.environ.get(_CC_COMMIT_SHA_ENV, "").strip().lower()
    if not raw:
        return None
    if not _FULL_SHA_RE.fullmatch(raw):
        return None
    return raw


def _candidate_holds_running_package(candidate: Path, package_init: Path) -> bool:
    """Return True iff ``candidate`` exposes the *same* ``__init__.py`` we imported.

    Naive ancestor-of-``__file__`` walks accept any parent ``.git`` they
    encounter, which is wrong for a wheel installed under a target
    repo's ``.venv/lib/.../site-packages/issue_orchestrator``: the
    walker would land on the *target* repo and report its SHA. The
    samefile check rules that out — the target repo doesn't carry a
    file that resolves to *our* running ``__init__.py``.

    Two layouts are accepted: src-layout (``src/issue_orchestrator/``)
    and flat layout (``issue_orchestrator/``). Anything else falls
    through and the resolver returns ``None``.
    """
    for relative in (
        candidate / "src" / "issue_orchestrator" / "__init__.py",
        candidate / "issue_orchestrator" / "__init__.py",
    ):
        try:
            if relative.samefile(package_init):
                return True
        except (OSError, ValueError):
            # samefile raises FileNotFoundError when either side is
            # missing; that just means this layout doesn't apply.
            continue
    return False


def _resolve_source_repo_root(
    start: Path,
    *,
    package_init: Path,
) -> Optional[Path]:
    """Walk up; return only the ``.git``-bearing candidate that *is* our source.

    Stops at the first ``.git`` we encounter — if that root does not
    expose our running package, we return ``None`` rather than keep
    walking. Continuing past an unrelated repo would risk landing in
    an even less related grandparent (``~/dev/.git`` covering many
    siblings, etc.) and feeding its SHA into the cache-buster.
    """
    package_init = package_init.resolve()
    current = start.resolve()
    for candidate in (current, *current.parents):
        if _resolve_git_dir(candidate) is None:
            continue
        if _candidate_holds_running_package(candidate, package_init):
            return candidate
        return None
    return None


_RUNNING_PACKAGE_INIT = Path(__file__).resolve().parent.parent / "__init__.py"
_PACKAGE_DIR = _RUNNING_PACKAGE_INIT.parent  # …/src/issue_orchestrator
_PACKAGE_REPO_ROOT = _resolve_source_repo_root(
    _PACKAGE_DIR,
    package_init=_RUNNING_PACKAGE_INIT,
)


def resolve_cc_commit_sha() -> Optional[str]:
    """Return the cc's source commit SHA, or ``None`` for non-source installs.

    Prefers the launcher-published commit when present. The Control Center
    normally imports from a frozen source snapshot that intentionally has no
    ``.git`` directory, so the launcher is the only component that can know
    the exact source commit copied into that snapshot. Source-checkout runs
    still fall back to the package-relative repo root.
    """
    env_sha = _commit_sha_from_env()
    if env_sha:
        return env_sha
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
