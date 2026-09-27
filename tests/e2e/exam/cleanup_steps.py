"""Run every cleanup step, then fail loudly (no e2e imports, so unit-testable)."""

from __future__ import annotations

import logging
from typing import Callable, Sequence

logger = logging.getLogger(__name__)


def run_all_steps(what: str, steps: Sequence[tuple[str, Callable[[], object]]]) -> None:
    """Run EVERY step even if one fails, then raise all failures together.

    One failed GitHub call (a network outage did this) must not skip the
    steps after it and strand a run's PRs, branches and issues.
    """
    failures: list[Exception] = []
    for name, step in steps:
        try:
            step()
        except Exception as exc:  # collected and re-raised below
            logger.error("[EXAM] cleanup step failed: %s: %s", name, exc)
            exc.add_note(f"cleanup step: {name}")
            failures.append(exc)
    if failures:
        raise ExceptionGroup(what, failures)
