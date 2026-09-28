"""Record an exam run's result BEFORE cleaning up (no e2e imports).

A run can take an hour; its scorecard is the point. Cleanup talks to GitHub
and can fail transiently — that must not cost the scorecard, and it must not
hide a failure of the run itself either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable, TypeVar

R = TypeVar("R")


async def run_recorded(
    run_case: Callable[[], Awaitable[R]],
    *,
    record: Callable[[R], Path],
    cleanup: Callable[[], None],
) -> tuple[R, Path]:
    """Run the case, write its artifacts, then clean up — always.

    * run and record succeed, cleanup fails: the artifacts are on disk and
      the cleanup failure is raised;
    * the run (or recording) fails: cleanup still runs; if it fails too,
      both are raised together, so neither masks the other.
    """
    try:
        result = await run_case()
        path = record(result)
    except BaseException as run_error:
        try:
            cleanup()
        except Exception as cleanup_error:
            raise BaseExceptionGroup(
                "the exam run and its cleanup both failed", [run_error, cleanup_error]
            ) from None
        raise
    cleanup()
    return result, path
