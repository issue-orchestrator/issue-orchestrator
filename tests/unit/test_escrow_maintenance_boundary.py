"""Unrelated maintenance cannot delete reserved escrow or prune its registration."""

from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree.api import remove_worktree
from issue_orchestrator.adapters.worktree.worktree_policy import ValidateOrDeletePolicy
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from .git_escrow_support import git_rig


def test_worktree_cleanup_cannot_delete_escrow_even_with_force(tmp_path):
    escrow = (
        tmp_path / ".issue-orchestrator/state/validated-work/1/e1-evidence/workspace"
    )
    escrow.mkdir(parents=True)
    evidence = escrow / "keep"
    evidence.write_text("unique evidence")
    with pytest.raises(ValueError, match="retention"):
        remove_worktree(escrow, force=True)
    with pytest.raises(ValueError, match="retention"):
        ValidateOrDeletePolicy().delete_worktree(escrow, tmp_path)
    assert evidence.read_text() == "unique evidence"


def test_session_retention_skips_escrow_workspace(tmp_path):
    escrow = (
        tmp_path / ".issue-orchestrator/state/validated-work/1/e1-evidence/workspace"
    )
    sessions = escrow / ".issue-orchestrator/sessions"
    for i in range(3):
        run = sessions / f"run-{i}"
        run.mkdir(parents=True)
        (run / "keep").write_text("evidence")
    assert FileSystemSessionOutput().prune_runs(escrow, keep=1) == []
    assert len(list(sessions.iterdir())) == 3


def test_git_prune_preserves_missing_escrow_workspace_registration(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    rig = git_rig(tmp_path)
    workspace = (
        rig.root / ".issue-orchestrator/state/validated-work/1/e1-evidence/workspace"
    )
    rig.run("worktree", "add", "--detach", str(workspace), rig.target)
    # Simulate lost filesystem; Git's registration is still needed for repair.
    import shutil

    shutil.rmtree(workspace)
    before = rig.run("worktree", "list", "--porcelain")
    rig.run("worktree", "prune", "--expire=now")
    assert rig.run("worktree", "list", "--porcelain") == before


def test_safe_unrelated_worktree_prune_still_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    rig = git_rig(tmp_path)
    workspace = tmp_path / "ordinary"
    rig.run("worktree", "add", "--detach", str(workspace), rig.target)
    import shutil

    shutil.rmtree(workspace)
    rig.run("worktree", "prune", "--expire=now")
    assert str(workspace) not in rig.run("worktree", "list", "--porcelain")
