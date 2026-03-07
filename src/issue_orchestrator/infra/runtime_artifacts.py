"""Registry for runtime-managed artifact paths.

These paths are writable runtime metadata (under .issue-orchestrator/
and .claude/) and should not be treated as user/source edits by
dirty-tree guardrails.
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
    ".claude/",
)


def is_runtime_managed_dirty_path(path: str) -> bool:
    """Return True when a dirty path is runtime-managed metadata."""
    normalized = path.replace("\\", "/")
    if normalized in RUNTIME_DIRTY_IGNORE_EXACT:
        return True
    return any(normalized.startswith(prefix) for prefix in RUNTIME_DIRTY_IGNORE_PREFIXES)


def filter_runtime_managed_dirty_paths(paths: list[str]) -> list[str]:
    """Return dirty paths excluding runtime-managed metadata files."""
    return [path for path in paths if not is_runtime_managed_dirty_path(path)]

