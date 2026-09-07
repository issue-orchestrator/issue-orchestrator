"""Real Git, filesystem leases and subprocesses; no agents or model calls."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from issue_orchestrator.adapters.budgeted_validation_git import BudgetedValidationGit
from issue_orchestrator.adapters.budgeted_validation_store import FileBudgetedValidationStore
from issue_orchestrator.control.budgeted_validation import BudgetedValidationCycle
from issue_orchestrator.domain.budgeted_validation import BudgetedValidationOutcome
from issue_orchestrator.execution.budgeted_validation_executor import BudgetedValidationCommandExecutor
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.process_group_command_runner import ProcessGroupCommandRunner
from issue_orchestrator.infra.budgeted_validation_config import parse_budgeted_validation
from issue_orchestrator.infra.process_table import ps_command, ps_env


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=root, text=True, capture_output=True, check=True).stdout.strip()


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "validation@example.invalid")
    git(root, "config", "user.name", "Validation fixture")
    git(root, "remote", "add", "origin", str(root))
    return root


def commit(root: Path, value: str, number: int) -> str:
    (root / "value").write_text(value)
    git(root, "add", "value")
    git(root, "commit", "--allow-empty", "-m", f"Change (#{number})")
    return git(root, "rev-parse", "HEAD")


def test_cross_worktree_claims_and_atomic_history_survive_reopen(tmp_path: Path):
    root = repository(tmp_path)
    commit(root, "green", 1)
    linked = tmp_path / "linked"
    git(root, "worktree", "add", "--detach", str(linked))
    first = BudgetedValidationGit(root, LocalCommandRunner())
    second = BudgetedValidationGit(linked, LocalCommandRunner())
    assert first.storage_directory() == second.storage_directory()
    store = FileBudgetedValidationStore(first.storage_directory())
    peer = FileBudgetedValidationStore(second.storage_directory())
    suite = parse_budgeted_validation({"agents": {"command": ["test"]}})["agents"]
    effects = []
    def owned(journal):
        assert not peer.run_exclusive(lambda _: effects.append("duplicate"))
        journal.write(suite, replace(journal.read(suite), diagnosis="retained evidence"))
    assert store.run_exclusive(owned)
    assert not effects
    assert peer.read(suite).diagnosis == "retained evidence"
    with pytest.raises(RuntimeError, match="lease"):
        peer.write(suite, peer.read(suite))
    assert peer.run_exclusive(lambda _: effects.append("next owner"))
    assert effects == ["next owner"]


def test_real_exact_commit_execution_and_bisection_preserve_the_callers_checkout(tmp_path: Path):
    root = repository(tmp_path)
    good = commit(root, "green", 1)
    history = BudgetedValidationGit(root, LocalCommandRunner())
    store = FileBudgetedValidationStore(history.storage_directory())
    command = [sys.executable, "-c", (
        "import json,os,pathlib,sys; bad=pathlib.Path('value').read_text()=='bad'; "
        "pathlib.Path(os.environ['IO_BUDGETED_VALIDATION_RESULT']).write_text(json.dumps({"
        "'status':'failed' if bad else 'passed','failed':['sentinel'] if bad else []})); sys.exit(1 if bad else 0)"
    )]
    suite = parse_budgeted_validation({"agents": {"command": command, "cadence": {"max_merges_since_success": 3}}})["agents"]
    executor = BudgetedValidationCommandExecutor(checkouts=history, runner=ProcessGroupCommandRunner(),
        directory=history.storage_directory(), environment=dict(os.environ))
    cycle = BudgetedValidationCycle(store=store, repository=history, executor=executor,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=timezone.utc))
    cycle.run((suite,))
    commit(root, "green", 2)
    culprit = commit(root, "bad", 3)
    bad = commit(root, "bad", 4)
    cycle.run((suite,))
    result = store.read(suite)
    assert result.last_success.probe.commit == good
    assert result.last_scheduled.probe.commit == bad
    assert result.first_bad_commit == culprit
    assert git(root, "rev-parse", "HEAD") == bad
    assert (root / "value").read_text() == "bad"
    assert len(git(root, "worktree", "list", "--porcelain").split("worktree ")) == 2
    assert all(Path(run.probe.evidence, "tests.log").is_file() for run in result.runs)
    assert not any(history.storage_directory().glob("runs/*/*/worktree"))


def test_changed_suite_definition_does_not_reuse_a_previous_green_history(tmp_path: Path):
    store = FileBudgetedValidationStore(tmp_path)
    suite = parse_budgeted_validation({"agents": {"command": ["old-test"]}})["agents"]
    store.run_exclusive(lambda journal: journal.write(suite, replace(journal.read(suite), diagnosis="old")))
    changed = replace(suite, command=("new-test",))
    assert store.read(changed).diagnosis == ""
    assert store.read(suite).diagnosis == "old"


def test_owned_command_timeout_is_unavailable_without_losing_diagnostics():
    result = ProcessGroupCommandRunner().run([sys.executable, "-c", "import signal; print('started', flush=True); signal.pause()"], timeout_seconds=1)
    assert result.timed_out
    assert "started" in result.stdout
    assert result.returncode != 0


@pytest.mark.parametrize("timeout", [False, True])
def test_owned_command_cleans_descendants_even_after_the_leader_exits(tmp_path: Path, timeout: bool):
    child_file = tmp_path / "child"
    command = """
import os, pathlib, signal, sys
read, write = os.pipe()
child = os.fork()
if child == 0:
    os.close(read)
    os.write(write, b'ready')
    signal.pause()
else:
    os.close(write)
    os.read(read, 5)
    pathlib.Path(sys.argv[1]).write_text(str(child))
    if sys.argv[2] == 'timeout':
        signal.pause()
"""
    result = ProcessGroupCommandRunner().run(
        [sys.executable, "-c", command, str(child_file), "timeout" if timeout else "exit"], timeout_seconds=1)
    assert result.timed_out == timeout
    # A killed orphan may briefly remain a zombie awaiting its system reaper.
    # It must have no executable process left in the owned group.
    child = child_file.read_text()
    state = subprocess.run(ps_command("-o", "stat=", "-p", child), env=ps_env(), capture_output=True, text=True, timeout=5)
    assert not state.stdout.strip() or state.stdout.strip().startswith("Z"), state.stdout


def test_refresh_uses_its_private_ref_without_replacing_an_unrelated_fetch_head(tmp_path: Path):
    root = repository(tmp_path)
    good = commit(root, "green", 1)
    git(root, "fetch", "origin", "main")
    bad = commit(root, "bad", 2)
    repository_adapter = BudgetedValidationGit(root, LocalCommandRunner())
    assert repository_adapter.head("main") == bad
    assert git(root, "rev-parse", "FETCH_HEAD") == good
