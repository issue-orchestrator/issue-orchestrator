"""One improver run over every engine Control Center runs (#7567).

The budgeted ``tech-lead-improver`` suite is due on engine activity, and an
improver run audits ONE engine: its findings cite that engine's staged inputs
(its start, its charter, its audit) and are validated against them alone. So
a sweep runs the improver once per in-scope engine, each with its own staged
inputs, run record and engine tag, rather than once over a merged view that
no finding could cite consistently. Every engine the inventory names is
audited; one engine's failure does not stop the others.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..contracts.improver_run import ImproverRunRecord
from ..domain.engine_activity import EngineRef
from ..ports.engine_activity import EngineInventory
from .improver_run import ImproverRun, ImproverRunRequest

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_UNAVAILABLE = 75


@dataclass(frozen=True)
class ImproverSweepRequest:
    outputs_repo: str
    exam_dir: Path | None
    window: timedelta
    log_tail_bytes: int
    #: An engine stopped longer ago than this is out of scope.
    recent: timedelta


@dataclass(frozen=True)
class ImproverSweepResult:
    engines: tuple[EngineRef, ...]
    runs: tuple[ImproverRunRecord, ...]
    apply: bool

    @property
    def exit_code(self) -> int:
        """Rejected findings anywhere fail the sweep; otherwise anything short
        of green (no engine, an unavailable run, effects still owed) is
        unavailable."""
        if not self.runs:
            return EXIT_UNAVAILABLE
        codes = {run.exit_code if self.apply else run.outcome.exit_code for run in self.runs}
        if EXIT_REJECTED in codes:
            return EXIT_REJECTED
        return EXIT_OK if codes == {EXIT_OK} else EXIT_UNAVAILABLE


class ImproverSweep:
    def __init__(
        self,
        *,
        inventory: EngineInventory,
        run_for: Callable[[EngineRef], ImproverRun],
        clock: Callable[[], datetime],
    ) -> None:
        self._inventory = inventory
        self._run_for = run_for
        self._clock = clock

    def sweep(self, request: ImproverSweepRequest, *, apply: bool = True) -> ImproverSweepResult:
        engines = self._inventory.engines(now=self._clock(), recent=request.recent)
        runs = tuple(
            self._run_for(engine).run(
                ImproverRunRequest(
                    engine=engine,
                    outputs_repo=request.outputs_repo,
                    exam_dir=request.exam_dir,
                    window=request.window,
                    log_tail_bytes=request.log_tail_bytes,
                ),
                apply=apply,
            )
            for engine in engines
        )
        return ImproverSweepResult(engines=engines, runs=runs, apply=apply)


__all__ = ["ImproverSweep", "ImproverSweepRequest", "ImproverSweepResult"]
