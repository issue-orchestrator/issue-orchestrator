"""Real kernel gates, process identities, and revocable startup capabilities."""

import copy
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from collections.abc import Iterator
from unittest.mock import Mock

import pytest

from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repo_lock_liveness import RepoLockLiveness
from issue_orchestrator.entrypoints.bootstrap_liveness import (
    held_repo_validated_work_liveness,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.repo_lock import (
    acquire_lock,
    held_repo_lock,
    held_startup_gate,
    release_lock,
)
from issue_orchestrator.infra.repo_lock_capability import HeldStartupGate
from issue_orchestrator.ports.command_runner import CommandResult, CommandRunner
from tests.process_group_run import signal_group


def test_production_liveness_factory_requires_and_uses_held_repo_gate(
    tmp_path: Path,
) -> None:
    config = Config()
    config.repo_root = tmp_path

    with pytest.raises(RuntimeError, match="successful local startup gate"):
        held_repo_validated_work_liveness(config)

    with held_repo_lock(tmp_path):
        liveness = held_repo_validated_work_liveness(config)
        identity = liveness.current()
        assert identity.pid == os.getpid()

    with pytest.raises(RuntimeError, match="released"):
        liveness.current()


@contextmanager
def child_owner(
    repo: Path, instance: str | None
) -> Iterator[tuple[subprocess.Popen[str], ProcessIdentity]]:
    script = """
import json, signal, sys
from dataclasses import asdict
from issue_orchestrator.infra.repo_lock import held_repo_lock, held_startup_gate
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repo_lock_liveness import RepoLockLiveness
signal.alarm(25)
instance = json.loads(sys.argv[2])
with held_repo_lock(sys.argv[1], instance_id=instance):
    current = RepoLockLiveness(held_startup_gate(sys.argv[1], LocalCommandRunner(), instance)).current()
    print(json.dumps(asdict(current)), flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(repo), json.dumps(instance)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        identity = ProcessIdentity(**json.loads(process.stdout.readline()))
        yield process, identity
    finally:
        # SIGKILL before reap retains the exact group identity even on assertion failure.
        signal_group(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)


@pytest.mark.parametrize("instance", [None, "A"])
def test_capability_requires_successful_held_startup_and_revokes(
    tmp_path: Path, instance: str | None
) -> None:
    runner = LocalCommandRunner()
    with pytest.raises(RuntimeError, match="startup gate"):
        held_startup_gate(tmp_path, runner, instance)
    with held_repo_lock(tmp_path, instance_id=instance):
        gate = held_startup_gate(tmp_path, runner, instance)
        live = RepoLockLiveness(gate)
        current = live.current()
        assert current.pid == os.getpid()
        assert not live.is_provably_dead(current)
        assert not live.is_provably_dead(
            replace(current, started_at="ambiguous same PID")
        )
        assert not live.is_provably_dead(
            replace(current, host="remote", pid=current.pid + 1)
        )
        assert live.is_provably_dead(replace(current, pid=current.pid + 1))
        assert live.is_provably_dead(
            replace(current, pid=current.pid + 1, instance_id=None)
        )
        with pytest.raises(TypeError):
            copy.copy(gate)
        with pytest.raises(TypeError):
            HeldStartupGate()
        assert not release_lock(tmp_path, pid=current.pid + 1, instance_id=instance)
        assert live.current() == current
    with pytest.raises(RuntimeError, match="released"):
        live.current()
    assert not live.is_provably_dead(replace(current, pid=current.pid + 1))
    with held_repo_lock(tmp_path, instance_id=instance):
        assert not live.is_provably_dead(replace(current, pid=current.pid + 1))


def test_live_different_named_process_blocks_then_dead_gate_proves_takeover(
    tmp_path: Path,
) -> None:
    with held_repo_lock(tmp_path, instance_id="A"):
        live = RepoLockLiveness(held_startup_gate(tmp_path, LocalCommandRunner(), "A"))
        with child_owner(tmp_path, "B") as (_, owner):
            assert not live.is_provably_dead(owner)
            # Removing the advertisement cannot change the kernel's ownership.
            (tmp_path / ".issue-orchestrator/locks/B.json").unlink()
            assert not live.is_provably_dead(owner)
        assert live.is_provably_dead(owner)
        assert not live.is_provably_dead(replace(owner, instance_id="../escape"))


@pytest.mark.parametrize(
    "previous,current", [(None, None), ("A", "A"), (None, "A"), ("A", None)]
)
def test_real_restart_gate_matrix(
    tmp_path: Path, previous: str | None, current: str | None
) -> None:
    with child_owner(tmp_path, previous) as (_, old_owner):
        pass
    with held_repo_lock(tmp_path, instance_id=current):
        live = RepoLockLiveness(
            held_startup_gate(tmp_path, LocalCommandRunner(), current)
        )
        assert live.is_provably_dead(old_owner)


@pytest.mark.parametrize(
    "result",
    [
        CommandResult(1, "", "failed"),
        CommandResult(0, "bad date", ""),
        CommandResult(0, "", "", timed_out=True),
    ],
)
def test_start_identity_read_failure_cannot_mint_capability(
    tmp_path: Path, result: CommandResult
) -> None:
    runner = Mock(spec=CommandRunner)
    runner.run.return_value = result
    with held_repo_lock(tmp_path):
        with pytest.raises((ValueError, RuntimeError)):
            held_startup_gate(tmp_path, runner)


def test_fork_cannot_use_or_release_parent_capability(tmp_path: Path) -> None:
    from tests.process_group_run import run_in_process_group

    script = """
import os, signal, sys
from threading import Event, Thread
from dataclasses import replace
from issue_orchestrator.infra.repo_lock import held_repo_lock, held_startup_gate, release_lock
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repo_lock_liveness import RepoLockLiveness
signal.alarm(20)
with held_repo_lock(sys.argv[1]):
    live = RepoLockLiveness(held_startup_gate(sys.argv[1], LocalCommandRunner()))
    parent = live.current()
    entered, finish = Event(), Event()
    class PausedRunner:
        def run(self, *args, **kwargs):
            entered.set()
            assert finish.wait(10)
            return LocalCommandRunner().run(*args, **kwargs)
    worker = Thread(target=lambda: held_startup_gate(sys.argv[1], PausedRunner()))
    worker.start()
    assert entered.wait(10)
    child = os.fork()
    if child == 0:
        try:
            assert not live.is_provably_dead(replace(parent, pid=parent.pid + 1))
            try:
                live.current()
            except RuntimeError:
                pass
            else:
                raise AssertionError("fork inherited current capability")
            assert not release_lock(sys.argv[1], pid=parent.pid)
            try:
                held_startup_gate(sys.argv[1], LocalCommandRunner())
            except RuntimeError:
                pass
            else:
                raise AssertionError("fork minted startup capability")
        except BaseException:
            os._exit(1)
        os._exit(0)
    try:
        _, status = os.waitpid(child, 0)
        assert status == 0
    finally:
        finish.set()
        worker.join(10)
    assert not worker.is_alive()
    assert live.current() == parent
"""
    result = run_in_process_group(
        [sys.executable, "-c", script, str(tmp_path)], timeout=25
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_cancelled_awaiter_retains_execution_until_child_is_reaped(
    tmp_path: Path,
) -> None:
    import asyncio
    from threading import Event

    from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
    from issue_orchestrator.execution.validated_work_execution import (
        LocalValidatedWorkExecutionOwner,
    )
    from issue_orchestrator.ports.validated_work_store import ValidatedWorkStore
    from tests.unit.threading_helpers import wait_for_event

    owner = LocalValidatedWorkExecutionOwner(Mock(spec=ValidatedWorkStore))
    ready, finish, completed = Event(), Event(), Event()

    errors: list[BaseException] = []

    def worker() -> None:
        lease = owner.try_enter("record")
        assert not isinstance(lease, RecordExecutionBusy)
        try:
            with lease as token:
                with child_owner(tmp_path, "child"):
                    owner.require_active(token, "record")
                    ready.set()
                    wait_for_event(finish, 10)
                # child_owner killed the group and reaped it before this effect.
                owner.require_active(token, "record")
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        await asyncio.to_thread(wait_for_event, ready, 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owner.try_enter("record") == RecordExecutionBusy("record")
    finally:
        finish.set()
        await asyncio.to_thread(wait_for_event, completed, 10)
    assert not errors
    lease = owner.try_enter("record")
    assert not isinstance(lease, RecordExecutionBusy)
    with lease as token:
        owner.require_active(token, "record")
