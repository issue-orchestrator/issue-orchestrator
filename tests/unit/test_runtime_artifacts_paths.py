"""Tests for the runtime-artifact path registry in ``runtime_artifacts``.

These filters are the shared source of truth between every dirty-tree guard
surface (``coding-done`` CLI, ``GitWorkingCopy.list_dirty_files``,
``CompletionRecordValidator``) and forced worktree cleanup.
"""

from __future__ import annotations

from issue_orchestrator.infra.runtime_artifacts import (
    RUNTIME_IGNORE_FILE,
    filter_runtime_managed_dirty_paths,
    is_cleanup_safe_untracked_path,
    is_runtime_managed_dirty_path,
    load_runtime_ignore_patterns,
    runtime_ignore_patterns,
)


# ---------------------------------------------------------------------------
# io's own source is not a runtime artifact anywhere (#7566)
# ---------------------------------------------------------------------------

IO_CLI_TOOL = "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"


def test_io_cli_tool_paths_are_not_runtime_metadata() -> None:
    """io no longer plants its tooling, so nothing excuses those paths.

    In io's own repo an untracked file there is an agent's new module; hiding
    it would let ``coding-done`` pass while the file never reaches the PR.
    """
    assert not is_runtime_managed_dirty_path(IO_CLI_TOOL)
    assert filter_runtime_managed_dirty_paths([IO_CLI_TOOL]) == [IO_CLI_TOOL]


def test_io_cli_tool_paths_are_not_cleanup_safe() -> None:
    """Forced worktree removal must not discard an untracked io source file."""
    assert not is_cleanup_safe_untracked_path(IO_CLI_TOOL)
    assert not is_cleanup_safe_untracked_path("src/")
    assert not is_cleanup_safe_untracked_path("src/issue_orchestrator/")


def test_runtime_metadata_filter_still_strips_its_targets() -> None:
    dirty = [
        ".issue-orchestrator/session-latest.json",
        ".issue-orchestrator/tool-homes/gradle/daemon/9.4.0/registry.bin",
        ".claude/settings.json",
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        "docs/README.md",
    ]
    kept = filter_runtime_managed_dirty_paths(dirty)
    assert kept == [
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        "docs/README.md",
    ]


def test_runtime_metadata_filter_strips_claude_scheduled_tasks_lock() -> None:
    assert is_runtime_managed_dirty_path(".claude/scheduled_tasks.lock")
    assert filter_runtime_managed_dirty_paths(
        [".claude/scheduled_tasks.lock", "src/app.py"]
    ) == ["src/app.py"]


def test_cleanup_safe_untracked_paths_cover_completed_session_artifacts() -> None:
    """Forced cleanup can discard known run artifacts without a second allowlist."""
    assert is_cleanup_safe_untracked_path(".agent-done-marker")
    assert is_cleanup_safe_untracked_path(".issue-orchestrator/validation/abc123.json")
    assert is_cleanup_safe_untracked_path(
        ".issue-orchestrator/sessions/run-1/validation-record.json"
    )
    assert is_cleanup_safe_untracked_path(
        ".issue-orchestrator/persistent-pairs/issue-1/coder/terminal.jsonl"
    )
    assert is_cleanup_safe_untracked_path("web/node_modules/.cache/state.json")
    assert is_cleanup_safe_untracked_path("src/pkg/__pycache__/module.pyc")
    assert is_cleanup_safe_untracked_path(".venv-semgrep/bin/semgrep")
    assert is_cleanup_safe_untracked_path("packages/vscode/dist/extension.js")


def test_cleanup_safe_untracked_paths_are_path_boundary_safe() -> None:
    assert not is_cleanup_safe_untracked_path(".issue-orchestrator/stateful-notes")
    assert not is_cleanup_safe_untracked_path(
        ".issue-orchestrator/review-report.md.backup"
    )
    assert not is_cleanup_safe_untracked_path("packages/app_node_modules/file.txt")


def test_loads_repo_local_runtime_ignore_file(tmp_path, caplog) -> None:
    ignore_file = tmp_path / RUNTIME_IGNORE_FILE
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text(
        "\n".join(
            [
                "# local runtime artifacts",
                "./.tool/runtime.lock",
                "tmp/runtime/",
                "!not-supported",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert load_runtime_ignore_patterns(tmp_path) == (
        ".tool/runtime.lock",
        "tmp/runtime/",
    )
    assert "Ignoring unsupported negated runtime-ignore pattern" in caplog.text
    assert is_runtime_managed_dirty_path(".tool/runtime.lock", tmp_path)
    assert is_runtime_managed_dirty_path("tmp/runtime/session.json", tmp_path)
    assert filter_runtime_managed_dirty_paths(
        [".tool/runtime.lock", "tmp/runtime/session.json", "src/app.py"],
        tmp_path,
    ) == ["src/app.py"]


def test_runtime_ignore_patterns_combines_builtins_and_repo_local(tmp_path) -> None:
    ignore_file = tmp_path / RUNTIME_IGNORE_FILE
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text("local-runtime/\n", encoding="utf-8")

    patterns = runtime_ignore_patterns(tmp_path)

    assert ".claude/scheduled_tasks.lock" in patterns
    assert ".issue-orchestrator/" in patterns
    assert "local-runtime/" in patterns


def test_runtime_ignore_file_supports_lightweight_globs(tmp_path) -> None:
    ignore_file = tmp_path / RUNTIME_IGNORE_FILE
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text("*.tmp\ncache/*.json\n", encoding="utf-8")

    assert is_runtime_managed_dirty_path("build.tmp", tmp_path)
    assert is_runtime_managed_dirty_path("cache/a.json", tmp_path)
    assert is_runtime_managed_dirty_path("cache/sub/b.json", tmp_path)
    assert not is_runtime_managed_dirty_path("cache/a.txt", tmp_path)


def test_runtime_ignore_file_drops_comments_blanks_and_negations(
    tmp_path, caplog
) -> None:
    ignore_file = tmp_path / RUNTIME_IGNORE_FILE
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text(
        "# comment\n\nruntime.lock\n!important.txt\n",
        encoding="utf-8",
    )

    assert load_runtime_ignore_patterns(tmp_path) == ("runtime.lock",)
    assert "Ignoring unsupported negated runtime-ignore pattern" in caplog.text
