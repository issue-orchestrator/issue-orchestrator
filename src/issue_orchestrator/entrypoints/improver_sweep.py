"""One improver run over every engine Control Center runs (#7567).

The budgeted ``tech-lead-improver`` suite is due on engine activity, and an
improver run audits ONE engine: its findings cite that engine's staged inputs
(its start, its charter, its audit) and are validated against them alone. So
a sweep runs the improver once per in-scope engine, each with its own staged
inputs, run record and engine tag, rather than once over a merged view that
no finding could cite consistently.

An engine is audited when it runs, or when it wrote its log since this
store's last ACCEPTED run of it (within ``recent`` for an engine never
accepted): an engine that did something after its last audit and then
stopped is still audited, however long ago it stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ..contracts.improver_run import ImproverRunRecord, RunOutcome
from ..domain.engine_activity import EngineRef, UnidentifiedEngine
from ..ports.engine_activity import EngineInventory
from ..ports.improver import ImproverRunReader
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
    #: How far back an engine never accepted before must have run.
    recent: timedelta


@dataclass(frozen=True)
class ImproverSweepResult:
    engines: tuple[EngineRef, ...]
    runs: tuple[ImproverRunRecord, ...]
    apply: bool
    #: Engines in scope that could not be audited (no start record to say
    #: which repository they work): the sweep is not green while any exists.
    unidentified: tuple[UnidentifiedEngine, ...] = ()

    @property
    def exit_code(self) -> int:
        """Rejected findings anywhere fail the sweep; otherwise anything short
        of green (no engine, an unidentified engine, an unavailable run,
        effects still owed) is unavailable."""
        codes = {run.exit_code if self.apply else run.outcome.exit_code for run in self.runs}
        if EXIT_REJECTED in codes:
            return EXIT_REJECTED
        green = bool(self.runs) and not self.unidentified and codes == {EXIT_OK}
        return EXIT_OK if green else EXIT_UNAVAILABLE


class ImproverSweep:
    def __init__(
        self,
        *,
        inventory: EngineInventory,
        runs: ImproverRunReader,
        run_for: Callable[[EngineRef], ImproverRun],
        clock: Callable[[], datetime],
    ) -> None:
        self._inventory = inventory
        self._runs = runs
        self._run_for = run_for
        self._clock = clock

    def sweep(self, request: ImproverSweepRequest, *, apply: bool = True) -> ImproverSweepResult:
        engines, unidentified = self._engines(self._clock() - request.recent)
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
        return ImproverSweepResult(engines=engines, runs=runs, apply=apply, unidentified=unidentified)

    def _engines(
        self, floor: datetime
    ) -> tuple[tuple[EngineRef, ...], tuple[UnidentifiedEngine, ...]]:
        """Each engine that ran since its last accepted run (or ``floor``)."""
        audited: dict[str, datetime] = {}
        for run in self._runs.runs():  # newest first
            if run.outcome is RunOutcome.ACCEPTED:
                audited.setdefault(run.engine_id, run.started_at)
        read = self._inventory.engines(since=min((floor, *audited.values())))
        engines = tuple(
            sighting.engine
            for sighting in read.sightings
            if sighting.active_since(audited.get(sighting.engine.engine_id, floor))
        )
        return engines, read.unidentified


__all__ = ["ImproverSweep", "ImproverSweepRequest", "ImproverSweepResult"]
