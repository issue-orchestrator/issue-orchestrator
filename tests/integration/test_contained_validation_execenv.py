"""Provider-free acceptance for crash-recoverable live-validation containment."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sys
import time

import pytest

from issue_orchestrator.adapters.condor.contained_validation import CondorContainedValidationRunner
from issue_orchestrator.adapters.condor.tools import CondorTools
from issue_orchestrator.domain.budgeted_validation import (
    BudgetedValidationOutcome, BudgetedValidationSuite, ValidationCadence,
)
from issue_orchestrator.execution.budgeted_validation_executor import BudgetedValidationCommandExecutor

pytestmark = [pytest.mark.timeout(240), pytest.mark.requires_infra]


class _FilesystemCheckouts:
    def create_checkout(self, commit: str, path: Path) -> None:
        path.mkdir()
        (path / "commit").write_text(commit)

    def remove_checkout(self, path: Path) -> None:
        shutil.rmtree(path)


def _suite(payload: Path) -> BudgetedValidationSuite:
    return BudgetedValidationSuite(
        name="containment-acceptance",
        command=(sys.executable, str(payload)),
        setup_command=(), cadence=ValidationCadence(),
        timeout_seconds=3, setup_timeout_seconds=1,
        branch="main", enabled=True,
    )


def _execute(directory: Path, payload: Path) -> None:
    executor = BudgetedValidationCommandExecutor(
        checkouts=_FilesystemCheckouts(),
        runner=CondorContainedValidationRunner(CondorTools.resolve()),
        directory=directory, environment=dict(os.environ),
    )
    executor.probe(_suite(payload), "acceptance-commit", "crash-recovery-run")


def _wait_until(predicate, detail: str, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(detail)


def test_coordinator_death_retains_checkout_until_cgroup_family_is_proved_gone(
    tmp_path: Path,
) -> None:
    child_pid = tmp_path / "detached.pid"
    payload = tmp_path / "detach.py"
    payload.write_text(
        "import os, pathlib, signal, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"    pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "    time.sleep(300)\n"
        "os.waitpid(child, 0)\n"
    )
    directory = tmp_path / "state"
    directory.mkdir()
    context = multiprocessing.get_context("fork")
    coordinator = context.Process(target=_execute, args=(directory, payload))
    coordinator.start()
    receipt = directory / "runs/containment-acceptance/crash-recovery-run/containment.json"
    workspace = directory / "runs/containment-acceptance/crash-recovery-run/worktree"
    _wait_until(
        lambda: receipt.is_file()
        and json.loads(receipt.read_text()).get("phase") == "submitted"
        and child_pid.is_file(),
        "contained job never reached the scheduler before coordinator death",
    )
    coordinator.kill()
    coordinator.join(10)
    assert not coordinator.is_alive()
    assert workspace.is_dir(), "coordinator death released the owned checkout"

    executor = BudgetedValidationCommandExecutor(
        checkouts=_FilesystemCheckouts(),
        runner=CondorContainedValidationRunner(CondorTools.resolve()),
        directory=directory, environment=dict(os.environ),
    )
    result = executor.resume(_suite(payload), "acceptance-commit", "crash-recovery-run")
    assert result.outcome is BudgetedValidationOutcome.UNAVAILABLE
    assert not workspace.exists(), "final scheduler proof did not release the checkout"
    detached = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(detached, 0)
