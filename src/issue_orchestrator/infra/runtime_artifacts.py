"""Registry for runtime-managed artifact paths.

Two categories of paths that dirty-tree guardrails must ignore:

1. **Runtime metadata** — session logs, caches, UI state under
   ``.issue-orchestrator/`` and ``.claude/``. These are written by the
   orchestrator *and the agent's own session* during normal work and
   never represent user-authored source changes. Filtered in every
   dirty-tree surface regardless of tracked/untracked status.

2. **Orchestrator-planted source** — files the orchestrator copies into
   every worktree at creation time (see
   ``adapters/worktree/_worktree_runtime.sync_cli_tools``). In the
   orchestrator's own repo these files are tracked source (legitimate
   dev edits *must* count as dirty). In a *foreign* target repo the
   same files are untracked plantings that should never register as
   dirty — otherwise every ``coding-done`` call in a foreign worktree
   fails the guard, the agent tries to work around it by editing
   ``.git/info/exclude`` (Claude Code's sensitive-file gate blocks
   interactively), and sessions silently burn 90 minutes to the
   session-level timeout. Filtered **only when untracked** so developer
   modifications in the orchestrator repo still fire the guard.
"""

from __future__ import annotations

RUNTIME_DIRTY_IGNORE_EXACT: frozenset[str] = frozenset({
    ".issue-orchestrator/session-latest.json",
    ".issue-orchestrator/ai-gate-state.json",
    ".issue-orchestrator/timeline.sqlite",
    ".issue-orchestrator/timeline.sqlite-shm",
    ".issue-orchestrator/timeline.sqlite-wal",
})

RUNTIME_DIRTY_IGNORE_PREFIXES: tuple[str, ...] = (
    ".issue-orchestrator/backups/",
    ".issue-orchestrator/sessions/",
    ".claude/",
)

# Paths ``sync_cli_tools`` plants into every worktree. Trailing slash is
# part of the prefix so the match doesn't accidentally swallow a future
# sibling like ``src/issue_orchestrator_tests/``.
ORCHESTRATOR_UNTRACKED_PLANTED_PREFIXES: tuple[str, ...] = (
    "src/issue_orchestrator/entrypoints/cli_tools/",
)


def is_runtime_managed_dirty_path(path: str) -> bool:
    """Return True when a dirty path is runtime-managed metadata.

    Applies regardless of tracked/untracked status — these paths are never
    source code in any repository, orchestrator or foreign.
    """
    normalized = path.replace("\\", "/")
    if normalized in RUNTIME_DIRTY_IGNORE_EXACT:
        return True
    return any(normalized.startswith(prefix) for prefix in RUNTIME_DIRTY_IGNORE_PREFIXES)


def filter_runtime_managed_dirty_paths(paths: list[str]) -> list[str]:
    """Return dirty paths excluding runtime-managed metadata files."""
    return [path for path in paths if not is_runtime_managed_dirty_path(path)]


def is_orchestrator_untracked_planted(path: str) -> bool:
    """Return True for paths ``sync_cli_tools`` plants into worktrees.

    Caller MUST only consult this for paths git reports as untracked.
    Tracked/modified versions of the same paths in the orchestrator's
    own repo are legitimate dev edits that the dirty-tree guard must
    still report.

    Accepts both individual-file paths
    (``src/issue_orchestrator/entrypoints/cli_tools/coding_done.py``)
    and the directory-summary form git emits when ``status --porcelain``
    collapses an entirely-untracked tree. A bare ``src/`` entry matches
    because a known planted prefix starts with it (and, being in the
    untracked list, can only mean the planted subtree is the lone
    untracked content under ``src/``).
    """
    normalized = path.replace("\\", "/")
    if not normalized:
        return False
    summary_form = normalized.endswith("/")
    for prefix in ORCHESTRATOR_UNTRACKED_PLANTED_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix):
            return True
        # Git's porcelain summary collapses untracked subtrees to their
        # topmost untracked directory. Accept the summary only when the
        # known planted prefix lies fully beneath it.
        if summary_form and prefix.startswith(normalized):
            return True
    return False


def filter_orchestrator_untracked_planted(paths: list[str]) -> list[str]:
    """Return only the untracked paths that are NOT orchestrator-planted.

    Input must already be scoped to git-untracked paths; the caller owns
    the tracked-vs-untracked classification (git status codes, separate
    ``ls-files --others`` invocation, etc.).
    """
    return [path for path in paths if not is_orchestrator_untracked_planted(path)]
