"""An agent whose worktree could not be rebased is told the RIGHT base (#8144).

In integration mode (and any ``worktrees.base_branch_override``, or a stack
successor) the worktree is based on a branch other than main. Telling the
agent to ``git rebase origin/main`` would replay that branch's commits into
its PR. Every launch path builds the warning from the worktree's own base.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.session_rework_launcher import build_rework_existing_work
from issue_orchestrator.control.session_worktree_briefing import (
    describe_worktree_state,
    rebase_conflict_warning,
)
from issue_orchestrator.ports.worktree_manager import WorktreeInfo


def test_the_warning_names_the_worktree_s_base_branch() -> None:
    warning = rebase_conflict_warning("integration")

    assert "git fetch origin integration && git rebase origin/integration" in warning
    assert "main" not in warning


def test_a_warning_without_its_base_fails_loudly() -> None:
    with pytest.raises(ValueError):
        rebase_conflict_warning(None)


def test_a_rework_whose_rebase_failed_is_told_its_base() -> None:
    info = WorktreeInfo(path=Path("/w"), branch_name="228-fix", rebase_failed=True, base_branch="integration")

    assert "origin/integration" in (build_rework_existing_work(info) or "")
    assert build_rework_existing_work(WorktreeInfo(path=Path("/w"), branch_name="228-fix")) is None


def test_a_coding_launch_whose_rebase_failed_is_told_its_base() -> None:
    working_copy = MagicMock()
    working_copy.get_commits_since.side_effect = RuntimeError("no existing work read in this test")

    briefing = describe_worktree_state(
        Path("/w"), working_copy, rebase_failed=True, base_branch="integration",
    )

    assert briefing is not None and "origin/integration" in briefing and "origin/main" not in briefing


def test_existing_work_is_judged_against_the_worktree_s_own_base() -> None:
    """A fresh integration-based worktree is ahead of main by all of integration:
    none of that is this agent's existing work."""
    working_copy = MagicMock()
    working_copy.get_commits_ahead_of.return_value = []

    assert describe_worktree_state(Path("/w"), working_copy, base_branch="integration") is None
    working_copy.get_commits_ahead_of.assert_called_once_with(Path("/w"), "integration")


def test_a_launch_without_its_worktree_s_base_fails_loudly() -> None:
    with pytest.raises(ValueError, match="base branch"):
        describe_worktree_state(Path("/w"), MagicMock())
