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

import json
import os
import time
from pathlib import Path
from typing import Optional

from .cc_snapshot import SNAPSHOT_DIR_NAME, SOURCE_METADATA_FILE
from .repo_identity import _resolve_git_dir, get_repo_head_sha

_CC_SNAPSHOT_ENV = "ISSUE_ORCHESTRATOR_CC_SNAPSHOT"
_CC_COMMIT_SHA_ENV = "ISSUE_ORCHESTRATOR_CC_COMMIT_SHA"


def _normalized_commit_sha(value: object) -> Optional[str]:
    raw = str(value).strip() if value is not None else ""
    if len(raw) != 40:
        return None
    if any(char not in "0123456789abcdefABCDEF" for char in raw):
        return None
    return raw.lower()


def _commit_sha_from_env() -> str | None:
    return _normalized_commit_sha(os.environ.get(_CC_COMMIT_SHA_ENV))


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


def _find_snapshot_metadata_path(package_init: Path) -> Optional[Path]:
    """Return source metadata path when imported from a frozen CC snapshot."""
    resolved_init = package_init.resolve()
    for candidate in resolved_init.parents:
        if candidate.name != "src":
            continue
        if candidate.parent.parent.name != SNAPSHOT_DIR_NAME:
            continue
        expected_init = candidate / "issue_orchestrator" / "__init__.py"
        try:
            if expected_init.samefile(resolved_init):
                return candidate.parent / SOURCE_METADATA_FILE
        except OSError:
            continue
    return None


def _resolve_snapshot_metadata_commit_sha(package_init: Path) -> Optional[str]:
    """Return the immutable source SHA carried by a frozen CC snapshot.

    The snapshot is a copy outside the source repo's ``.git``. PR #6266
    correctly prevents that copied package from claiming the live repo's
    ``src/issue_orchestrator/__init__.py`` as the same file, so the
    snapshot carries its source identity in a metadata file written at
    snapshot creation time.
    """
    if not os.environ.get(_CC_SNAPSHOT_ENV):
        return None
    metadata_path = _find_snapshot_metadata_path(package_init)
    if metadata_path is None:
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict):
        return None
    return _normalized_commit_sha(metadata.get("commit_sha"))


def resolve_cc_commit_sha() -> Optional[str]:
    """Return the cc's source commit SHA, or ``None`` for non-source installs.

    Prefers the launcher-published commit when present. The Control Center
    normally imports from a frozen source snapshot that intentionally has no
    ``.git`` directory; if that env contract is absent, snapshot metadata is
    the immutable source identity copied into the frozen package. Source-
    checkout runs still fall back to the package-relative repo root.
    """
    env_sha = _commit_sha_from_env()
    if env_sha:
        return env_sha
    snapshot_sha = _resolve_snapshot_metadata_commit_sha(_RUNNING_PACKAGE_INIT)
    if snapshot_sha:
        return snapshot_sha
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
