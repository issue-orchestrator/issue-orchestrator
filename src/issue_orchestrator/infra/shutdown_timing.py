"""Shared, interruptible timing policy for Repository Engine shutdown."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import os
import time
from typing import Callable, Protocol

DEFAULT_ENGINE_GRACEFUL_TIMEOUT_SECONDS = 120
_FORCE_SIGNAL_WAIT_SECONDS = 3.0


class StopAction(Enum):
    """Action selected by one checkpoint of a Repository Engine stop."""

    WAIT = "wait"
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    FORCE = "force"
    ABORT = "abort"


@dataclass(frozen=True)
class StopPolicySnapshot:
    """Current operator policy for an in-flight Repository Engine stop."""

    graceful_timeout_seconds: float
    force: bool = False
    abort: bool = False


class StopPolicy(Protocol):
    """Behavior-level source of live stop policy."""

    def snapshot(self) -> StopPolicySnapshot:
        """Return the policy that should govern the next wait checkpoint."""
        ...


@dataclass(frozen=True)
class StaticStopPolicy:
    """Fixed policy used by ordinary CLI and single-engine stop calls."""

    graceful_timeout_seconds: float
    force: bool = False

    def snapshot(self) -> StopPolicySnapshot:
        return StopPolicySnapshot(
            graceful_timeout_seconds=self.graceful_timeout_seconds,
            force=self.force,
        )


@dataclass(frozen=True)
class StopBudgetCheckpoint:
    """Decision and remaining shared graceful budget at one instant."""

    action: StopAction
    remaining_seconds: float


def process_is_alive(pid: int) -> bool:
    """Return whether a process still accepts signal-zero probes."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InterruptibleStopBudget:
    """Own one elapsed-time budget while observing live policy updates."""

    def __init__(
        self,
        policy: StopPolicy,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policy = policy
        self._clock = clock
        self._sleeper = sleeper
        self._started_at = clock()

    def checkpoint(self) -> StopBudgetCheckpoint:
        policy = self._policy.snapshot()
        elapsed = self._clock() - self._started_at
        remaining = max(0.0, policy.graceful_timeout_seconds - elapsed)
        if policy.abort:
            action = StopAction.ABORT
        elif policy.force:
            action = StopAction.FORCE
        elif remaining <= 0:
            action = StopAction.TIMED_OUT
        else:
            action = StopAction.WAIT
        return StopBudgetCheckpoint(action=action, remaining_seconds=remaining)

    def wait_for_exit(
        self,
        pid: int,
        process_probe: Callable[[int], bool],
    ) -> StopAction:
        """Wait until exit or the live policy interrupts the graceful budget."""
        while process_probe(pid):
            checkpoint = self.checkpoint()
            if checkpoint.action is not StopAction.WAIT:
                return checkpoint.action
            self._sleeper(min(0.1, checkpoint.remaining_seconds))
        return StopAction.EXITED


class InterruptibleStopController:
    """Own the complete graceful wait and its force/abort transitions."""

    def __init__(
        self,
        policy: StopPolicy,
        *,
        pid: int,
        force_requested: bool,
        force_on_timeout: bool,
        request_graceful: Callable[[], bool],
        terminate: Callable[[], None],
        force_stop: Callable[[], bool],
        on_stopped: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        process_probe: Callable[[int], bool] = process_is_alive,
    ) -> None:
        self._budget = InterruptibleStopBudget(
            policy,
            clock=clock,
            sleeper=sleeper,
        )
        self._pid = pid
        self._force_requested = force_requested
        self._force_on_timeout = force_on_timeout
        self._request_graceful = request_graceful
        self._terminate = terminate
        self._force_stop = force_stop
        self._on_stopped = on_stopped
        self._process_probe = process_probe

    def stop(self) -> bool:
        """Execute one interruptible stop using one elapsed-time budget."""
        initial_action = self._budget.checkpoint().action
        if initial_action is StopAction.ABORT:
            raise StopAborted("Stop aborted by operator policy")
        if self._force_requested or initial_action is StopAction.FORCE:
            return self._force_stop()
        if not self._request_graceful():
            self._terminate()

        wait_result = self._budget.wait_for_exit(self._pid, self._process_probe)
        if wait_result is StopAction.EXITED:
            self._on_stopped()
            return True
        if wait_result is StopAction.ABORT:
            raise StopAborted("Stop aborted by operator policy")
        if wait_result is StopAction.FORCE or (
            wait_result is StopAction.TIMED_OUT and self._force_on_timeout
        ):
            return self._force_stop()
        return False


class StopAborted(RuntimeError):
    """Raised when an operator aborts the stop currently being attempted."""


def signal_exit_poll_iterations(
    *, force: bool, grace_seconds: float
) -> int:
    """Return 100ms poll iterations before supervisor escalation."""
    wait_seconds = {
        True: _FORCE_SIGNAL_WAIT_SECONDS,
        False: grace_seconds,
    }[force]
    return max(1, int(wait_seconds * 10))
