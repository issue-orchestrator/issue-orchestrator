"""Exact Repository Engine stop adapter backed only by Linux pidfds."""

from __future__ import annotations

import signal
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ..domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
    StopEngineCommand,
    StopEngineOutcome,
    StopEngineStatus,
)
from ..infra.linux_pidfd import LinuxPidfdRuntime
from ..infra.process_incarnation import is_linux_process_incarnation
from ..infra.repo_identity import normalize_repo_root
from ..infra.repo_lock import LockInfo, read_lock


_FORCE_EXIT_WAIT_SECONDS = 10.0
AdvertisementReader = Callable[[Path, str | None], LockInfo | None]


class _PidfdRuntime(Protocol):
    def supported(self) -> bool: ...
    def open(self, pid: int) -> int: ...
    def process_incarnation(self, pid: int) -> str: ...
    def send(self, handle: int, sig: int | signal.Signals) -> None: ...
    def wait_for_exit(self, handle: int, timeout_seconds: float) -> bool: ...
    def close(self, handle: int) -> None: ...


class LinuxPidfdIncarnationStop:
    """Pin one PID lifetime; never use HTTP, process groups, ports, or lock cleanup."""

    def __init__(
        self,
        runtime: _PidfdRuntime | None = None,
        *,
        read_advertisement: AdvertisementReader = read_lock,
    ) -> None:
        self._runtime = runtime or LinuxPidfdRuntime()
        self._read_advertisement = read_advertisement

    def stop_availability(self, engine: EngineIdentity) -> EngineStopAvailability:
        if type(engine) is not EngineIdentity:
            raise TypeError("incarnation stop requires a typed engine identity")
        return (
            EngineStopAvailability.AVAILABLE
            if is_linux_process_incarnation(engine.process.started_at)
            and self._runtime.supported()
            else EngineStopAvailability.EXACT_TARGET_UNAVAILABLE
        )

    def stop_expected(self, command: StopEngineCommand) -> StopEngineOutcome:
        if type(command) is not StopEngineCommand:
            raise TypeError("incarnation stop requires a typed stop command")
        engine = command.engine
        if self.stop_availability(engine) is not EngineStopAvailability.AVAILABLE:
            return self._outcome(
                StopEngineStatus.FAILED,
                engine,
                "Linux pidfd exact-process stop is unavailable",
            )
        root = normalize_repo_root(engine.repo_root)
        try:
            advertised = self._read_advertisement(root, engine.instance_id)
        except (OSError, KeyError, TypeError, ValueError) as error:
            return self._failed(
                engine, f"Could not verify the expected engine advertisement: {error}"
            )
        if advertised is not None and not _advertises(advertised, engine, root):
            return self._changed(engine)
        try:
            handle = self._runtime.open(engine.process.pid)
        except ProcessLookupError:
            try:
                replacement = self._read_advertisement(root, engine.instance_id)
            except (OSError, KeyError, TypeError, ValueError) as error:
                return self._failed(
                    engine, f"Could not recheck the engine advertisement: {error}"
                )
            return (
                self._changed(engine)
                if replacement is not None
                and not _advertises(replacement, engine, root)
                else self._stopped(engine, "Expected engine process is already gone")
            )
        except OSError as error:
            return self._failed(
                engine, f"Could not pin expected engine process: {error}"
            )
        outcome = self._stop_pinned(handle, command, advertised)
        try:
            self._runtime.close(handle)
        except OSError as error:
            return self._failed(
                engine, f"Could not close exact process handle: {error}"
            )
        return outcome

    def _stop_pinned(
        self,
        handle: int,
        command: StopEngineCommand,
        advertised: LockInfo | None,
    ) -> StopEngineOutcome:
        engine = command.engine
        if advertised is None:
            return self._changed(engine)
        try:
            try:
                incarnation = self._runtime.process_incarnation(engine.process.pid)
            except FileNotFoundError:
                if self._runtime.wait_for_exit(handle, 0):
                    return self._stopped(
                        engine, "Expected engine process is already gone"
                    )
                raise
            if incarnation != engine.process.started_at:
                return self._changed(engine)
            self._runtime.send(handle, signal.SIGTERM)
            if self._runtime.wait_for_exit(handle, command.graceful_timeout_seconds):
                return self._stopped(engine, "Expected engine stopped gracefully")
            if not command.force_on_timeout:
                return self._failed(
                    engine, "Expected engine did not stop before timeout"
                )
            self._runtime.send(handle, signal.SIGKILL)
            if self._runtime.wait_for_exit(handle, _FORCE_EXIT_WAIT_SECONDS):
                return self._stopped(
                    engine,
                    "Expected engine was forcibly stopped after graceful timeout",
                )
            return self._failed(engine, "Expected engine did not exit after SIGKILL")
        except ProcessLookupError:
            return self._stopped(engine, "Expected engine process is already gone")
        except (OSError, ValueError) as error:
            return self._failed(engine, f"Exact engine stop failed: {error}")

    @staticmethod
    def _outcome(
        status: StopEngineStatus, engine: EngineIdentity, message: str
    ) -> StopEngineOutcome:
        return StopEngineOutcome(status, engine, message)

    def _changed(self, engine: EngineIdentity) -> StopEngineOutcome:
        return self._outcome(
            StopEngineStatus.TARGET_CHANGED,
            engine,
            "Repository Engine instance no longer advertises the expected process",
        )

    def _stopped(self, engine: EngineIdentity, message: str) -> StopEngineOutcome:
        return self._outcome(StopEngineStatus.STOPPED, engine, message)

    def _failed(self, engine: EngineIdentity, message: str) -> StopEngineOutcome:
        return self._outcome(StopEngineStatus.FAILED, engine, message)


def _advertises(info: LockInfo, engine: EngineIdentity, root: Path) -> bool:
    try:
        advertised_root = normalize_repo_root(info.repo_root)
    except (OSError, TypeError, ValueError):
        return False
    return (
        advertised_root == root
        and info.instance_id == engine.instance_id
        and info.pid == engine.process.pid
    )
