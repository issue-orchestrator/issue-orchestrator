"""The improver's run store, shared by every worktree of the repository (#7490)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_run import ImproverRunRecord, RunOutcome
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_run_store import FileImproverRunStore

NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


def _record(run_id: str, at: datetime, outcome: RunOutcome = RunOutcome.REJECTED) -> ImproverRunRecord:
    return ImproverRunRecord(
        run_id=run_id, started_at=at, finished_at=at, outcome=outcome, detail="",
        engine_id="repo-a-b", audited_repo="a/b", outputs_repo="a/b", run_dir="/x",
    )


def test_runs_read_back_newest_first(tmp_path: Path) -> None:
    store = FileImproverRunStore(tmp_path)
    for run_id, at in (("old", NOW - timedelta(days=1)), ("new", NOW)):
        store.new_run_dir(run_id)
        store.record(_record(run_id, at))

    assert [r.run_id for r in store.runs()] == ["new", "old"]


def test_only_an_accepted_run_owes_findings(tmp_path: Path) -> None:
    store = FileImproverRunStore(tmp_path)
    store.new_run_dir("r")

    with pytest.raises(ValueError, match="owes no findings"):
        store.accepted_findings(_record("r", NOW))


def test_every_worktree_of_a_repository_shares_one_store(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x")
    git("worktree", "add", "-q", "--detach", str(tmp_path / "wt"))

    main = FileImproverRunStore.for_checkout(repo, LocalCommandRunner())
    main.new_run_dir("r")
    main.record(_record("r", NOW))

    assert [r.run_id for r in FileImproverRunStore.for_checkout(tmp_path / "wt", LocalCommandRunner()).runs()] == ["r"]


def test_one_run_or_apply_holds_the_store_at_a_time(tmp_path: Path) -> None:
    from issue_orchestrator.ports.improver import ImproverStoreBusy

    first, second = FileImproverRunStore(tmp_path), FileImproverRunStore(tmp_path)

    with first.exclusive():
        with pytest.raises(ImproverStoreBusy):
            with second.exclusive():
                pass
    with second.exclusive():
        pass
