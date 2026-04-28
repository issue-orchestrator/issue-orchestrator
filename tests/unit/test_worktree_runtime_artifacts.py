"""Tests for worktree runtime artifact Git visibility."""

from pathlib import Path

from issue_orchestrator.adapters.worktree._worktree_runtime import (
    _worktree_git_exclude_paths,
)
from issue_orchestrator.infra.runtime_artifacts import RUNTIME_IGNORE_FILE


def test_worktree_git_exclude_paths_include_claude_scheduled_tasks_lock(tmp_path):
    paths = _worktree_git_exclude_paths(tmp_path, [])

    assert Path(".claude/scheduled_tasks.lock") in paths
    assert RUNTIME_IGNORE_FILE in paths


def test_worktree_git_exclude_paths_include_repo_local_runtime_ignore_file(tmp_path):
    ignore_file = tmp_path / RUNTIME_IGNORE_FILE
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text(".tool/runtime.lock\ncache/runtime/\n", encoding="utf-8")

    paths = _worktree_git_exclude_paths(tmp_path, [Path("synced/tool.py")])

    assert Path(".tool/runtime.lock") in paths
    assert Path("cache/runtime") in paths
    assert Path("synced/tool.py") in paths
