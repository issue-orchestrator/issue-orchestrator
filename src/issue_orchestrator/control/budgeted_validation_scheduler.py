"""Keep periodic validation observation off the repository engine tick."""

from collections.abc import Callable

from ..ports.budgeted_validation_worker import BudgetedValidationWorker


class BudgetedValidationScheduler:
    def __init__(self, worker: BudgetedValidationWorker, *, clock: Callable[[], float], check_interval_seconds: float) -> None:
        if check_interval_seconds <= 0:
            raise ValueError("Validation observation interval must be positive")
        self._worker = worker
        self._clock = clock
        self._interval = check_interval_seconds
        self._next_check = 0.0

    def tick(self) -> None:
        now = self._clock()
        if now < self._next_check or self._worker.running():
            return
        self._next_check = now + self._interval
        self._worker.start()
