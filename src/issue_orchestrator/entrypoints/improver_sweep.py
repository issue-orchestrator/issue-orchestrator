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
from typing import Protocol
from datetime import datetime, timedelta
from pathlib import Path

from ..contracts.improver_run import ImproverRunRecord
from ..domain.engine_activity import EngineRef, UnidentifiedEngine
from ..ports.engine_activity import EngineInventory
from ..ports.improver import ImproverRunStore
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
    #: A blind sweep's hidden open issues (``ImproverRunRequest.excluded_open_issues``).
    excluded_open_issues: frozenset[int] = frozenset()


@dataclass(frozen=True)
class ImproverSweepResult:
    engines: tuple[EngineRef, ...]
    runs: tuple[ImproverRunRecord, ...]
    apply: bool
    #: Engines in scope that could not be audited (no start record to say
    #: which repository they work): the sweep is not green while any exists.
    unidentified: tuple[UnidentifiedEngine, ...] = ()
    #: Engines that ran lately but not since their last accepted run (an
    #: operator's manual run, say, already audited what they did).
    already_audited: tuple[EngineRef, ...] = ()
    #: Accepted runs that still owe GitHub an effect after the sweep: an
    #: accepted finding is not done until it is applied.
    owed_by: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        """Rejected findings anywhere fail the sweep; otherwise anything short
        of green (no engine, an unidentified engine, an unavailable run,
        effects still owed) is unavailable."""
        codes = {run.exit_code if self.apply else run.outcome.exit_code for run in self.runs}
        if EXIT_REJECTED in codes:
            return EXIT_REJECTED
        if self.unidentified or codes - {EXIT_OK} or (self.apply and self.owed_by):
            return EXIT_UNAVAILABLE
        # Green when every engine in scope was audited, now or by an accepted
        # run since it last wrote; no engine in scope at all is not.
        return EXIT_OK if self.runs or self.already_audited else EXIT_UNAVAILABLE


class OwedEffects(Protocol):
    """The effect owner (:class:`~..execution.improver_effect_applier.ImproverEffects`)."""

    def apply_pending(self) -> tuple[ImproverRunRecord, ...]: ...

    def owing_runs(self) -> tuple[str, ...]: ...


class ImproverSweep:
    def __init__(
        self,
        *,
        inventory: EngineInventory,
        runs: ImproverRunStore,
        effects: OwedEffects,
        run_for: Callable[[EngineRef], ImproverRun],
        clock: Callable[[], datetime],
    ) -> None:
        self._inventory = inventory
        self._runs = runs
        self._effects = effects
        self._run_for = run_for
        self._clock = clock

    def sweep(self, request: ImproverSweepRequest, *, apply: bool = True) -> ImproverSweepResult:
        engines, already, unidentified = self._engines(self._clock() - request.recent)
        runs = tuple(
            self._run_for(engine).run(
                ImproverRunRequest(
                    engine=engine,
                    outputs_repo=request.outputs_repo,
                    exam_dir=request.exam_dir,
                    window=request.window,
                    log_tail_bytes=request.log_tail_bytes,
                    excluded_open_issues=request.excluded_open_issues,
                ),
                apply=apply,
            )
            for engine in engines
        )
        return ImproverSweepResult(
            engines=engines, runs=runs, apply=apply, unidentified=unidentified,
            already_audited=already, owed_by=self._settle(apply=apply),
        )

    def _settle(self, *, apply: bool) -> tuple[str, ...]:
        """Apply what accepted runs still owe (each audit run applies first,
        but a sweep with nothing to audit runs none), and what remains owed."""
        with self._runs.exclusive():
            if apply:
                self._effects.apply_pending()
            return self._effects.owing_runs()

    def _engines(
        self, floor: datetime
    ) -> tuple[tuple[EngineRef, ...], tuple[EngineRef, ...], tuple[UnidentifiedEngine, ...]]:
        """The engines to audit, those an accepted run audited since they
        last ran, and those that cannot be identified. An engine never
        accepted counts if it ran since ``floor``. A blind run audits nothing
        (``ImproverRunRecord.is_engine_audit``)."""
        audited: dict[str, datetime] = {}
        accepted = [run for run in self._runs.runs() if run.is_engine_audit]
        for run in accepted:  # newest first
            audited.setdefault(run.engine_id, run.started_at)
        # Back to the OLDEST accepted run: an engine that last wrote before
        # its latest accepted run (a manual run audited it) is still seen, so
        # the sweep can say it is already audited rather than not in scope.
        read = self._inventory.engines(since=min((floor, *(r.started_at for r in accepted))))
        due = tuple(
            s for s in read.sightings if s.active_since(audited.get(s.engine.engine_id, floor))
        )
        already = tuple(
            s.engine for s in read.sightings if s not in due and s.engine.engine_id in audited
        )
        return tuple(s.engine for s in due), already, read.unidentified


__all__ = ["ImproverSweep", "ImproverSweepRequest", "ImproverSweepResult"]
