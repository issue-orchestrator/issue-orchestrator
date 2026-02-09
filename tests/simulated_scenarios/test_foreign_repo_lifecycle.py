"""Foreign repo lifecycle integration test.

Exercises the full orchestrator lifecycle (coder → completion → validation →
review exchange → PR creation) against a *foreign* repository — one that has
no orchestrator source tree, no `.venv`, no `Makefile`.  Uses the **real**
`GitWorktreeManager` adapter so the worktree is created via `git worktree add`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager

from .conftest import (
    ScriptSessionRunner,
    StubWorkingCopy,
    build_config,
    build_orchestrator,
    run_until,
)
from .scenario_dsl import script


def _init_foreign_repo(tmp_path: Path) -> Path:
    """Create a minimal foreign git repo with an ``origin`` remote.

    Layout::

        tmp_path/
          origin.git/    ← bare repo (acts as remote)
          foreign-repo/  ← working clone with just a README
    """
    bare = tmp_path / "origin.git"
    clone = tmp_path / "foreign-repo"

    # Create bare repo as origin
    subprocess.run(
        ["git", "init", "--bare", str(bare)],
        check=True,
        capture_output=True,
    )

    # Clone it to get a working copy
    subprocess.run(
        ["git", "clone", str(bare), str(clone)],
        check=True,
        capture_output=True,
    )

    # Ensure the default branch is named "main"
    subprocess.run(
        ["git", "checkout", "-b", "main"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )

    # Create initial commit so main branch exists
    readme = clone / "README.md"
    readme.write_text("# Foreign repo\n")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@test.com",
         "commit", "-m", "Initial commit"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(clone),
        check=True,
        capture_output=True,
    )

    return clone


@pytest.fixture()
def foreign_repo(tmp_path: Path) -> Path:
    return _init_foreign_repo(tmp_path)


@pytest.mark.integration
def test_foreign_repo_full_lifecycle(foreign_repo: Path, tmp_path: Path) -> None:
    """Full orchestrator lifecycle against a repo with no orchestrator files."""
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()

    # --- Config ---
    config = build_config(
        foreign_repo,
        coder_command=script("coder_dual_mode.sh"),
        reviewer_command=script("reviewer_ok.sh", prompt=True),
        review_exchange_mode="via-local-loop",
        validation_cmd=script("validate_pass.sh"),
    )
    config.worktree_base = worktree_base

    # --- Issue ---
    issue = Issue(
        number=1,
        title="Foreign repo test issue",
        labels=["simulated-scenario", "agent:coder"],
    )

    # --- Build orchestrator with real GitWorktreeManager ---
    orch, repo_host, events, _timeline_reader = build_orchestrator(
        foreign_repo,
        [issue],
        config,
        worktree_manager=GitWorktreeManager(),  # type: ignore[arg-type]  # duck-typed
        working_copy=StubWorkingCopy(),
        runner=ScriptSessionRunner(),
    )

    # --- Run ---
    run_until(orch, lambda: not orch.state.active_sessions, max_ticks=15)

    # --- Assertions ---
    emitted = {e.name for e in events.events}
    assert EventName.SESSION_STARTED in emitted
    assert EventName.SESSION_COMPLETED in emitted
    assert EventName.REVIEW_EXCHANGE_STARTED in emitted
    assert EventName.REVIEW_EXCHANGE_COMPLETED in emitted
    assert EventName.ISSUE_PR_CREATED in emitted

    # PR was created
    assert repo_host.get_pr(100) is not None, "Expected PR #100 to be created"

    # The worktree was a real git worktree (.git is a file, not a directory)
    history = orch.state.session_history
    assert history, "Expected session history to be non-empty"
    worktree_path = history[0].worktree_path
    assert worktree_path is not None
    git_entry = Path(worktree_path) / ".git"
    assert git_entry.exists(), ".git should exist in worktree"
    assert git_entry.is_file(), ".git should be a file (worktree), not a directory"

    # No self-referential artifacts: foreign repo should NOT have .venv or
    # src/issue_orchestrator/ in the worktree (those come from venv symlink
    # and cli_tools sync, which only happen if the source repo has them).
    wt = Path(worktree_path)
    venv = wt / ".venv"
    if venv.exists():
        # If .venv exists it must be a symlink (installed by finalize), not a real dir
        assert venv.is_symlink(), ".venv in worktree should be a symlink, not a copy"
    assert not (wt / "src" / "issue_orchestrator").exists(), (
        "Foreign repo worktree should not contain src/issue_orchestrator/"
    )

    # Clean up
    close = getattr(orch, "close", None)
    if callable(close):
        close()
