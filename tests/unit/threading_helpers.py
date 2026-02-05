from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ThreadResult:
    result: Any | None = None
    error: BaseException | None = None

    def unwrap(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


def run_in_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[threading.Thread, ThreadResult]:
    """Run a function in a thread and capture its result/exception."""
    result = ThreadResult()

    def target() -> None:
        try:
            result.result = fn(*args, **kwargs)
        except BaseException as exc:  # pragma: no cover - defensive
            result.error = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def join_or_fail(thread: threading.Thread, timeout: float, *, label: str = "thread") -> None:
    """Join a thread and assert it completed."""
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"{label} did not finish within {timeout}s"


def wait_for_event(event: threading.Event, timeout: float, *, label: str = "event") -> None:
    """Wait for a threading.Event and assert it was signaled."""
    signaled = event.wait(timeout=timeout)
    assert signaled, f"{label} not signaled within {timeout}s"


async def wait_for_async_event(event: asyncio.Event, timeout: float, *, label: str = "event") -> None:
    """Wait for an asyncio.Event with a timeout and assert it was signaled."""
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:  # pragma: no cover - defensive
        raise AssertionError(f"{label} not signaled within {timeout}s") from exc
