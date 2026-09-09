"""Real Linux pidfd process-lifetime behavior."""

from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from issue_orchestrator.adapters.repository_engine_incarnation import (
    LinuxPidfdIncarnationStop,
)
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    StopEngineCommand,
    StopEngineStatus,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.infra.linux_pidfd import LinuxPidfdRuntime
from issue_orchestrator.infra.repo_lock import (
    LockInfo,
    held_repo_lock,
    held_startup_gate,
    read_lock,
)
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repo_lock_liveness import RepoLockLiveness


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd only")


def _engine(repo: Path, process: subprocess.Popen[str]) -> EngineIdentity:
    incarnation = LinuxPidfdRuntime().process_incarnation(process.pid)
    identity = ProcessIdentity("local", process.pid, incarnation, None)
    return EngineIdentity(str(repo), None, "local", "default", identity)


def _advertisement(engine: EngineIdentity) -> LockInfo:
    return LockInfo(
        engine.repo_root,
        engine.process.pid,
        "lock-write-time",
        None,
        str(Path(engine.repo_root) / ".issue-orchestrator/state"),
    )


def _spawn(script: str) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "ready"
    return process


def _adapter(engine: EngineIdentity) -> LinuxPidfdIncarnationStop:
    return LinuxPidfdIncarnationStop(
        read_advertisement=lambda _root, _instance: _advertisement(engine)
    )


def test_kernel_birth_identity_matches_the_claim_identity_format(
    tmp_path: Path,
) -> None:
    runtime = LinuxPidfdRuntime()
    with held_repo_lock(tmp_path):
        current = RepoLockLiveness(
            held_startup_gate(tmp_path, LocalCommandRunner())
        ).current()

        assert runtime.process_incarnation(os.getpid()) == current.started_at


def test_real_pidfd_observes_graceful_sigterm_exit(tmp_path: Path) -> None:
    process = _spawn(
        "import signal,time; signal.signal(signal.SIGTERM, lambda *_: exit(0)); "
        "print('ready', flush=True); time.sleep(30)"
    )
    try:
        engine = _engine(tmp_path, process)
        outcome = _adapter(engine).stop_expected(
            StopEngineCommand(engine, "test", "graceful exact stop", 2, True)
        )
        assert outcome.status is StopEngineStatus.STOPPED
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_real_pidfd_forces_a_sigterm_resistant_process(tmp_path: Path) -> None:
    process = _spawn(
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(30)"
    )
    try:
        engine = _engine(tmp_path, process)
        outcome = _adapter(engine).stop_expected(
            StopEngineCommand(engine, "test", "forced exact stop", 0.05, True)
        )
        assert outcome.status is StopEngineStatus.STOPPED
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_force_remains_pinned_when_instance_advertises_a_replacement(
    tmp_path: Path,
) -> None:
    original = _spawn(
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print('ready', flush=True); time.sleep(30)"
    )
    replacement: subprocess.Popen[str] | None = None
    advertisement: LockInfo | None = None

    class ReplacingRuntime(LinuxPidfdRuntime):
        replaced = False

        def wait_for_exit(self, handle: int, timeout_seconds: float) -> bool:
            nonlocal replacement, advertisement
            exited = super().wait_for_exit(handle, timeout_seconds)
            if not exited and not self.replaced:
                self.replaced = True
                replacement = _spawn(
                    "import time; print('ready', flush=True); time.sleep(30)"
                )
                advertisement = _advertisement(_engine(tmp_path, replacement))
            return exited

    try:
        engine = _engine(tmp_path, original)
        advertisement = _advertisement(engine)
        adapter = LinuxPidfdIncarnationStop(
            ReplacingRuntime(),
            read_advertisement=lambda _root, _instance: advertisement,
        )

        outcome = adapter.stop_expected(
            StopEngineCommand(engine, "test", "force only the pinned owner", 0.05, True)
        )

        assert outcome.status is StopEngineStatus.STOPPED
        assert original.wait(timeout=5) == -signal.SIGKILL
        assert replacement is not None
        assert replacement.poll() is None
    finally:
        for process in (original, replacement):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


_ENGINE_SHUTDOWN_CHILD = """
import asyncio
import json
import sys
from dataclasses import asdict

from issue_orchestrator.entrypoints.run_orchestrator import _install_shutdown_signal_handlers
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.repo_lock_liveness import RepoLockLiveness
from issue_orchestrator.infra import shutdown_signals
from issue_orchestrator.infra.repo_lock import held_repo_lock, held_startup_gate

assert shutdown_signals.block_shutdown_signals() is True
assert shutdown_signals.begin_shutdown_watch() is True

async def main():
    stopped = asyncio.Event()

    class Engine:
        def request_shutdown(self):
            stopped.set()

    with held_repo_lock(sys.argv[1]):
        identity = RepoLockLiveness(
            held_startup_gate(sys.argv[1], LocalCommandRunner())
        ).current()
        _install_shutdown_signal_handlers(Engine(), lambda: None)
        print(json.dumps(asdict(identity)), flush=True)
        await asyncio.wait_for(stopped.wait(), timeout=15)

asyncio.run(main())
"""


def test_pidfd_sigterm_uses_repository_engine_shutdown_and_releases_gate(
    tmp_path: Path,
) -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", _ENGINE_SHUTDOWN_CHILD, str(tmp_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        identity = ProcessIdentity(**json.loads(process.stdout.readline()))
        engine = EngineIdentity(str(tmp_path), None, identity.host, "default", identity)

        outcome = LinuxPidfdIncarnationStop().stop_expected(
            StopEngineCommand(engine, "test", "production graceful shutdown", 5, True)
        )

        assert outcome.status is StopEngineStatus.STOPPED
        assert process.wait(timeout=10) == 0
        assert read_lock(tmp_path) is None
        with held_repo_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
