"""One owner for periodic test coverage and bounded regression diagnosis."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from ..domain.budgeted_validation import (
    BisectRange, BudgetedValidationHistory, BudgetedValidationOutcome,
    BudgetedValidationProbe, BudgetedValidationRun, BudgetedValidationSuite,
)
from ..ports.budgeted_validation import (
    BudgetedValidationExecutor, BudgetedValidationJournal, BudgetedValidationRepository,
    BudgetedValidationStore,
)
from ..ports.contained_validation import ContainedValidationPending


class BudgetedValidationCycle:
    def __init__(self, *, store: BudgetedValidationStore, repository: BudgetedValidationRepository,
                 executor: BudgetedValidationExecutor, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._repository = repository
        self._executor = executor
        self._clock = clock

    def run(self, suites: tuple[BudgetedValidationSuite, ...], *, force: bool = False) -> bool:
        """One repository-wide lease coalesces all callers, modes and worktrees."""
        def execute(journal: BudgetedValidationJournal) -> None:
            heads: dict[str, str] = {}
            pending_names: set[str] = set()
            for pending in journal.pending():
                pending_names.add(pending.suite.name)
                self._resume_recovery(journal, pending.suite, pending.history)
            for suite in suites:
                if suite.enabled and suite.name not in pending_names:
                    if suite.branch not in heads:
                        heads[suite.branch] = self._repository.head(suite.branch)
                    self._run_suite(journal, suite, heads[suite.branch], force=force)
        return self._store.run_exclusive(execute)

    def _run_suite(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
                   head: str, *, force: bool) -> None:
        history = journal.read(suite)
        now = self._clock()
        pending = history.latest
        if pending is not None and pending.finished_at is None:
            self._resume_pending(journal, suite, history)
            return
        watermark = history.scheduling_watermark
        since_attempt = self._repository.changes(watermark.probe.commit, head) if watermark else ()
        if not force and not history.scheduled_due(
            now=now, cadence=suite.cadence, head=head,
            integrations_since_attempt=self._repository.merged_count(since_attempt),
        ):
            return
        history = self._probe(journal, suite, history, head, "scheduled")
        if history.latest is None:
            raise RuntimeError("Executed probe produced no history")
        if history.latest.finished_at is None:
            return
        failure = history.latest.probe
        if not failure.is_failure:
            return
        self._start_diagnosis(journal, suite, history)

    def _resume_pending(self, journal: BudgetedValidationJournal,
                        suite: BudgetedValidationSuite,
                        history: BudgetedValidationHistory) -> None:
        pending = history.latest
        if pending is None or pending.finished_at is not None or pending.suite != suite:
            raise ValueError("resume requires the exact durable pending suite")
        try:
            probe = self._executor.resume(suite, pending.probe.commit, pending.id)
        except ContainedValidationPending:
            return
        resumed = replace(pending, finished_at=self._clock(), probe=probe)
        history = history.complete_latest(resumed)
        journal.write(suite, history)
        if resumed.purpose == "scheduled":
            if resumed.probe.is_failure:
                self._start_diagnosis(journal, suite, history)
            return
        self._continue_diagnosis(journal, suite, history)

    def _resume_recovery(self, journal: BudgetedValidationJournal,
                         suite: BudgetedValidationSuite,
                         history: BudgetedValidationHistory) -> None:
        latest = history.latest
        if latest is None:
            raise ValueError("recoverable validation has no latest run")
        if latest.finished_at is None:
            self._resume_pending(journal, suite, history)
            return
        regression = history.regression
        if regression is None or regression.diagnosis_complete:
            raise ValueError("validation is not recoverable")
        if latest.id == regression.failed.id:
            self._start_diagnosis(journal, suite, history)
        else:
            self._continue_diagnosis(journal, suite, history)

    def _start_diagnosis(self, journal: BudgetedValidationJournal,
                         suite: BudgetedValidationSuite,
                         history: BudgetedValidationHistory) -> None:
        failure = history.latest
        if (failure is None or failure.finished_at is None
                or failure.purpose != "scheduled" or not failure.probe.is_failure):
            raise ValueError("diagnosis requires a completed scheduled failure")
        green = history.last_success
        changes = self._repository.changes(
            green.probe.commit, failure.probe.commit,
        ) if green else ()
        if green is not None and not changes:
            journal.write(suite, history.with_diagnosis("The previously green commit now fails; no new integration exists to bisect. Investigate reproducibility or the environment."))
            return
        if green is None or not failure.probe.failure_signature:
            diagnosis = "No comparable successful baseline or stable failure signature; retain the failing commit for investigation."
            journal.write(suite, history.with_diagnosis(diagnosis))
            return
        self._diagnose(journal, suite, history, (green.probe.commit, *changes), failure.probe)

    def _continue_diagnosis(self, journal: BudgetedValidationJournal,
                            suite: BudgetedValidationSuite,
                            history: BudgetedValidationHistory) -> None:
        regression = history.regression
        if regression is None or regression.last_green_commit is None:
            raise ValueError("pending diagnosis lost its durable regression baseline")
        changes = self._repository.changes(
            regression.last_green_commit, regression.failed.probe.commit,
        )
        self._diagnose(
            journal, suite, history,
            (regression.last_green_commit, *changes), regression.failed.probe,
        )

    def _probe(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
               history: BudgetedValidationHistory, commit: str, purpose: str) -> BudgetedValidationHistory:
        started = self._clock()
        run_id = uuid4().hex
        pending = BudgetedValidationRun(run_id, started, None,
            BudgetedValidationProbe(commit, BudgetedValidationOutcome.UNAVAILABLE, ""), purpose,
            suite)
        journal.write(suite, history.append(pending))
        try:
            probe = self._executor.probe(suite, commit, run_id)
        except ContainedValidationPending:
            return history.append(pending)
        if probe.commit != commit:
            raise ValueError("Validation result belongs to a different commit")
        complete = BudgetedValidationRun(run_id, started, self._clock(), probe, purpose, suite)
        history = history.append(complete)
        journal.write(suite, history)
        return history

    def _diagnose(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
                  history: BudgetedValidationHistory, commits: tuple[str, ...], failure: BudgetedValidationProbe) -> None:
        diagnostics = _diagnostic_runs(history, failure.commit)
        cursor = 0
        history, repeated, cursor = self._diagnostic_step(
            journal, suite, history, diagnostics, cursor,
            failure.commit, "reproduce",
        )
        if repeated.finished_at is None:
            return
        if not repeated.probe.reproduces(failure):
            self._inconclusive(journal, suite, history, "Failure did not reproduce consistently; no commit accused.")
            return
        history, baseline, cursor = self._diagnostic_step(
            journal, suite, history, diagnostics, cursor,
            commits[0], "verify-baseline",
        )
        if baseline.finished_at is None:
            return
        if not baseline.probe.is_success:
            self._inconclusive(journal, suite, history, "Previous green failed under the current test environment; no commit accused.")
            return
        state = BisectRange(commits)
        while state.midpoint is not None:
            midpoint = state.midpoint
            history, result, cursor = self._diagnostic_step(
                journal, suite, history, diagnostics, cursor,
                midpoint, "bisect",
            )
            if result.finished_at is None:
                return
            probe = result.probe
            if not probe.narrows(failure):
                self._inconclusive(journal, suite, history, "Bisection encountered unavailable coverage or a different failure; original range retained.")
                return
            state = state.observe(midpoint, probe.outcome)
        journal.write(suite, history.with_diagnosis(
            f"Reproduced regression; first failing integration {state.first_bad}; last green {commits[0]}; first observed failure {failure.commit}.",
            first_bad=state.first_bad))

    def _diagnostic_step(
        self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
        history: BudgetedValidationHistory,
        diagnostics: tuple[BudgetedValidationRun, ...], cursor: int,
        commit: str, purpose: str,
    ) -> tuple[BudgetedValidationHistory, BudgetedValidationRun, int]:
        if cursor < len(diagnostics):
            run = diagnostics[cursor]
            if run.purpose != purpose or run.probe.commit != commit:
                raise ValueError("durable diagnosis no longer matches its derived next step")
            return history, run, cursor + 1
        history = self._probe(journal, suite, history, commit, purpose)
        run = history.latest
        if run is None:
            raise RuntimeError("Diagnostic probe produced no result")
        return history, run, cursor + 1

    def _inconclusive(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
                      history: BudgetedValidationHistory, detail: str) -> None:
        last = history.latest
        if last is None:
            raise RuntimeError("Diagnosis has no preceding probe")
        ambiguous = replace(last, probe=replace(last.probe, outcome=BudgetedValidationOutcome.INCONCLUSIVE))
        journal.write(suite, replace(history.with_diagnosis(detail), latest=ambiguous))


def _diagnostic_runs(history: BudgetedValidationHistory,
                     failed_commit: str) -> tuple[BudgetedValidationRun, ...]:
    failed = history.regression.failed if history.regression is not None else None
    if failed is None or failed.probe.commit != failed_commit:
        raise ValueError("diagnosis has no matching durable regression")
    try:
        index = next(index for index, run in enumerate(history.runs) if run.id == failed.id)
    except StopIteration as error:
        raise ValueError("regression run is absent from durable history") from error
    return tuple(history.runs[index + 1:])
