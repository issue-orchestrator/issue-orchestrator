"""The pruner must spare a run whose artifacts a launch still needs (#7273).

A validation retry reads its launch inputs out of the ORIGINAL run, and worktree
preparation prunes old runs BEFORE that copy happens. Repeated pre-spawn refusals
pile up newer, allocation-only directories, so once retention is exceeded the
pruner deleted the only trusted copy of `tech-lead-data` -- and every later retry
was permanently refused (round 8 finding 2).
"""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def _run(worktree: Path, name: str) -> Path:
    run_dir = worktree / ".issue-orchestrator" / "sessions" / name
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({"session_name": name}))
    return run_dir


def test_the_preserved_run_survives_and_the_others_are_pruned(tmp_path: Path) -> None:
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    oldest = _run(worktree, "20260101T000000000000Z__issue-1")
    for n in range(3):
        _run(worktree, f"2026021{n}T000000000000Z__coding-{n + 2}")

    removed = FileSystemSessionOutput().prune_runs(
        worktree, 1, preserve_run_dir=oldest
    )

    assert oldest.exists(), "the run holding the launch inputs was pruned"
    assert removed, "nothing was pruned, so the test proves nothing"


def test_without_the_preservation_the_same_run_goes(tmp_path: Path) -> None:
    """The premise: this run IS otherwise prunable."""
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    oldest = _run(worktree, "20260101T000000000000Z__issue-1")
    for n in range(3):
        _run(worktree, f"2026021{n}T000000000000Z__coding-{n + 2}")

    FileSystemSessionOutput().prune_runs(worktree, 1)

    assert not oldest.exists()


def test_preserving_does_not_grow_the_retention_count(tmp_path: Path) -> None:
    """A preserved run counts toward `keep`, it does not add a slot."""
    worktree = tmp_path / "checkout"
    worktree.mkdir()
    oldest = _run(worktree, "20260101T000000000000Z__issue-1")
    newer = [
        _run(worktree, f"2026021{n}T000000000000Z__coding-{n + 2}") for n in range(3)
    ]

    FileSystemSessionOutput().prune_runs(worktree, 2, preserve_run_dir=oldest)

    survivors = [d for d in (oldest, *newer) if d.exists()]
    assert len(survivors) == 2, f"retention grew: {[d.name for d in survivors]}"
    assert oldest in survivors
