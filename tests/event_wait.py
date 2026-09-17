"""Waiting on an event, with a backstop that names the event (#7148, #7264).

Shared between the backend-agnostic lane contract and the backend suites that
inherit it, because both were re-deriving the same deadline loop and one of them
drifted into asserting a cause its observation could not establish.

It lives at ``tests/`` rather than inside either suite so neither has to import
the other's private helper to get it.
"""

from __future__ import annotations

import time
from collections.abc import Callable

__all__ = ["POLL_SECONDS", "await_event"]

#: Poll gap while waiting. Granularity, not coordination: there is no ack
#: channel from the kernel for "this pid is gone" or from the filesystem for
#: "this file appeared".
POLL_SECONDS = 0.05


def await_event(
    predicate: Callable[[], bool], *, backstop_seconds: float
) -> bool:
    """Wait for an event to have happened; report whether it did.

    The return value is the answer, never an assertion: the caller owns the
    message that names ITS event, which is the point of #7264 -- a shared
    helper that raised would have to guess which event the caller was waiting
    for, and guessing is exactly what produced a flake diagnosed as buffering.

    The clock is read BEFORE the predicate, so ``True`` means the event was
    observed strictly inside the window. A probe-first loop would also report an
    event that became true only during an overscheduled final sleep -- for a
    process-reaping wait that is a quietly relaxed assertion, the "true because
    a sleep was long enough" this tree refuses. The cost is one 50ms poll gap of
    sensitivity at a boundary three orders of magnitude further out.
    """
    deadline = time.monotonic() + backstop_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(POLL_SECONDS)
    return False
