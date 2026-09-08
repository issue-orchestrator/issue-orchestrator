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
            for suite in suites:
                if suite.enabled:
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
            try:
                probe = self._executor.resume(
                    suite, pending.probe.commit, pending.id,
                )
            except ContainedValidationPending:
                return
            resumed = replace(pending, finished_at=now, probe=probe)
            history = history.append(resumed)
            journal.write(suite, history)
            if resumed.purpose != "scheduled":
                journal.write(suite, history.with_diagnosis(
                    "Interrupted diagnosis was recovered without duplicate submission; "
                    "the original regression remains for the next bounded diagnosis."
                ))
                return
        green = history.last_success
        watermark = history.scheduling_watermark
        since_attempt = self._repository.changes(watermark.probe.commit, head) if watermark else ()
        if not force and not history.scheduled_due(
            now=now, cadence=suite.cadence, head=head,
            integrations_since_attempt=self._repository.merged_count(since_attempt),
        ):
            return
        changes = self._repository.changes(green.probe.commit, head) if green else ()
        history = self._probe(journal, suite, history, head, "scheduled")
        if history.latest is None:
            raise RuntimeError("Executed probe produced no history")
        if history.latest.finished_at is None:
            return
        failure = history.latest.probe
        if not failure.is_failure:
            return
        if green is not None and not changes:
            journal.write(suite, history.with_diagnosis("The previously green commit now fails; no new integration exists to bisect. Investigate reproducibility or the environment."))
            return
        if green is None or not failure.failure_signature:
            diagnosis = "No comparable successful baseline or stable failure signature; retain the failing commit for investigation."
            journal.write(suite, history.with_diagnosis(diagnosis))
            return
        self._diagnose(journal, suite, history, (green.probe.commit, *changes), failure)

    def _probe(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
               history: BudgetedValidationHistory, commit: str, purpose: str) -> BudgetedValidationHistory:
        started = self._clock()
        run_id = uuid4().hex
        pending = BudgetedValidationRun(run_id, started, None,
            BudgetedValidationProbe(commit, BudgetedValidationOutcome.UNAVAILABLE, ""), purpose)
        journal.write(suite, history.append(pending))
        try:
            probe = self._executor.probe(suite, commit, run_id)
        except ContainedValidationPending:
            return history.append(pending)
        if probe.commit != commit:
            raise ValueError("Validation result belongs to a different commit")
        complete = BudgetedValidationRun(run_id, started, self._clock(), probe, purpose)
        history = history.append(complete)
        journal.write(suite, history)
        return history

    def _diagnose(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
                  history: BudgetedValidationHistory, commits: tuple[str, ...], failure: BudgetedValidationProbe) -> None:
        history = self._probe(journal, suite, history, failure.commit, "reproduce")
        repeated = history.latest
        if repeated is not None and repeated.finished_at is None:
            return
        if repeated is None or not repeated.probe.reproduces(failure):
            self._inconclusive(journal, suite, history, "Failure did not reproduce consistently; no commit accused.")
            return
        history = self._probe(journal, suite, history, commits[0], "verify-baseline")
        baseline = history.latest
        if baseline is not None and baseline.finished_at is None:
            return
        if baseline is None or not baseline.probe.is_success:
            self._inconclusive(journal, suite, history, "Previous green failed under the current test environment; no commit accused.")
            return
        state = BisectRange(commits)
        while state.midpoint is not None:
            midpoint = state.midpoint
            history = self._probe(journal, suite, history, midpoint, "bisect")
            result = history.latest
            if result is None:
                raise RuntimeError("Bisection probe produced no result")
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

    def _inconclusive(self, journal: BudgetedValidationJournal, suite: BudgetedValidationSuite,
                      history: BudgetedValidationHistory, detail: str) -> None:
        last = history.latest
        if last is None:
            raise RuntimeError("Diagnosis has no preceding probe")
        ambiguous = replace(last, probe=replace(last.probe, outcome=BudgetedValidationOutcome.INCONCLUSIVE))
        journal.write(suite, replace(history.with_diagnosis(detail), latest=ambiguous))
