"""Tests for the orchestrator-planted-path filters in runtime_artifacts.

These filters are the shared source of truth between every dirty-tree
guard surface (``coding-done`` CLI, ``GitWorkingCopy.list_dirty_files``,
``CompletionRecordValidator``). The *only* reason the planted-paths
filter exists: ``sync_cli_tools`` copies ``src/issue_orchestrator/
entrypoints/cli_tools/*`` into every worktree, and in a foreign target
repo (Kotlin, etc.) those files appear as untracked — tripping the
dirty-tree guard on every ``coding-done`` call. The filter removes them
only when untracked so the orchestrator's own repo still catches real
developer edits.
"""

from __future__ import annotations

from issue_orchestrator.infra.runtime_artifacts import (
    RUNTIME_IGNORE_FILE,
    filter_orchestrator_untracked_planted,
    filter_runtime_managed_dirty_paths,
    is_orchestrator_untracked_planted,
    is_runtime_managed_dirty_path,
    load_runtime_ignore_patterns,
    runtime_ignore_patterns,
)


# ---------------------------------------------------------------------------
# is_orchestrator_untracked_planted
# ---------------------------------------------------------------------------


def test_matches_individual_planted_file() -> None:
    assert is_orchestrator_untracked_planted(
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"
    )


def test_matches_planted_prefix_itself() -> None:
    assert is_orchestrator_untracked_planted(
        "src/issue_orchestrator/entrypoints/cli_tools/"
    )


def test_matches_summary_dir_form_from_porcelain() -> None:
    """git status --porcelain collapses entire untracked trees to the topmost dir."""
    # The agent's reported error was ``?? src/`` — git had collapsed the
    # whole untracked subtree to its root. The helper must recognise this
    # because the only known planted root lives under src/.
    assert is_orchestrator_untracked_planted("src/")
    assert is_orchestrator_untracked_planted("src/issue_orchestrator/")


def test_rejects_unrelated_src_files() -> None:
    """A real source file under src/ (non-cli_tools) is not planted."""
    assert not is_orchestrator_untracked_planted("src/main.py")
    assert not is_orchestrator_untracked_planted("src/foo/bar.py")


def test_rejects_unrelated_paths() -> None:
    assert not is_orchestrator_untracked_planted("README.md")
    assert not is_orchestrator_untracked_planted(".githooks/pre-push")
    assert not is_orchestrator_untracked_planted("")


def test_rejects_sibling_prefix_outside_planted_tree() -> None:
    """``src/issue_orchestrator_tests/`` shares a prefix but is not planted."""
    assert not is_orchestrator_untracked_planted("src/issue_orchestrator_tests/foo.py")


def test_normalizes_windows_separators() -> None:
    assert is_orchestrator_untracked_planted(
        "src\\issue_orchestrator\\entrypoints\\cli_tools\\coding_done.py"
    )


def test_filter_strips_planted_keeps_others() -> None:
    paths = [
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        "src/issue_orchestrator/entrypoints/cli_tools/reviewer_done.py",
        "docs/README.md",
        "tests/test_foo.py",
    ]

    kept = filter_orchestrator_untracked_planted(paths)

    assert kept == ["docs/README.md", "tests/test_foo.py"]


# ---------------------------------------------------------------------------
# Existing runtime-metadata filter — unchanged, but cover the invariant that
# it does NOT strip planted paths (distinct category).
# ---------------------------------------------------------------------------


def test_runtime_metadata_filter_does_not_strip_planted_paths() -> None:
    """The two filters are orthogonal; neither can cover for the other.

    ``filter_runtime_managed_dirty_paths`` only covers
    ``.issue-orchestrator/`` / ``.claude/`` metadata. If it silently
    swallowed planted paths, a future caller that only runs the runtime
    filter would accidentally hide them regardless of tracked/untracked
    status — which would mask developer edits in the orchestrator's own
    repo.
    """
    planted = "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py"
    assert not is_runtime_managed_dirty_path(planted)
    assert filter_runtime_managed_dirty_paths([planted]) == [planted]


def test_runtime_metadata_filter_still_strips_its_targets() -> None:
    """Regression: adding the planted filter must not break existing behaviour."""
    dirty = [
        ".issue-orchestrator/session-latest.json",
        ".issue-orchestrator/tool-homes/gradle/daemon/9.4.0/registry.bin",
        ".claude/settings.json",
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        "docs/README.md",
    ]
    kept = filter_runtime_managed_dirty_paths(dirty)
    # Planted file is not runtime metadata; survives this filter.
    assert kept == [
        "src/issue_orchestrator/entrypoints/cli_tools/coding_done.py",
        "docs/README.md",
    ]


def test_runtime_metadata_filter_strips_claude_scheduled_tasks_lock() -> None:
    assert is_runtime_managed_dirty_path(".claude/scheduled_tasks.lock")
    assert filter_runtime_managed_dirty_paths(
        [".claude/scheduled_tasks.lock", "src/app.py"]
    ) == ["src/app.py"]


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
