"""Cost-bounded validation cadence and deterministic regression narrowing."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


@dataclass(frozen=True, slots=True)
class ValidationCadence:
    max_merges_since_success: int = 10
    max_delay_hours: int = 24

    def __post_init__(self) -> None:
        if type(self.max_merges_since_success) is not int or self.max_merges_since_success <= 0:
            raise ValueError("max_merges_since_success must be a positive integer")
        if type(self.max_delay_hours) is not int or self.max_delay_hours <= 0:
            raise ValueError("max_delay_hours must be a positive integer")

    def due(self, *, now: datetime, last_success_at: datetime | None, merges_since_success: int, changed: bool) -> bool:
        """A success covers unchanged code; either configured bound covers new code."""
        if now.tzinfo is None or (last_success_at is not None and last_success_at.tzinfo is None):
            raise ValueError("Validation timestamps must include a timezone")
        if merges_since_success < 0:
            raise ValueError("merges_since_success cannot be negative")
        if last_success_at is None:
            return True
        return changed and (
            merges_since_success >= self.max_merges_since_success
            or now >= last_success_at + timedelta(hours=self.max_delay_hours)
        )


@dataclass(frozen=True, slots=True)
class BudgetedValidationSuite:
    name: str
    command: tuple[str, ...]
    setup_command: tuple[str, ...]
    cadence: ValidationCadence
    timeout_seconds: int
    setup_timeout_seconds: int
    branch: str
    enabled: bool
    issue_agent_label: str = "agent:backend"


class BudgetedValidationOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    INCONCLUSIVE = "inconclusive"

    @property
    def is_failure(self) -> bool:
        return self is BudgetedValidationOutcome.FAILED


@dataclass(frozen=True, slots=True)
class BisectRange:
    """An ordered first-parent range whose endpoints were verified good and bad.

    Callers must reproduce the same failure and verify the green endpoint under
    the same test definition before constructing this range. Unavailable or
    inconsistent outcomes never move either endpoint.
    """

    commits: tuple[str, ...]
    good_index: int = 0
    bad_index: int = -1

    def __post_init__(self) -> None:
        if self.bad_index == -1:
            object.__setattr__(self, "bad_index", len(self.commits) - 1)
        if not 0 <= self.good_index < self.bad_index < len(self.commits):
            raise ValueError("Bisection requires distinct verified good and bad endpoints")
        if len(set(self.commits)) != len(self.commits) or not all(self.commits):
            raise ValueError("Bisection commits must be distinct and nonempty")

    @property
    def midpoint(self) -> str | None:
        if self.bad_index - self.good_index == 1:
            return None
        return self.commits[(self.good_index + self.bad_index) // 2]

    @property
    def first_bad(self) -> str | None:
        return self.commits[self.bad_index] if self.midpoint is None else None

    @property
    def remaining_runs(self) -> int:
        """ceil(log2(number of suspect changes)), derived rather than configured."""
        return (self.bad_index - self.good_index - 1).bit_length()

    def observe(self, commit: str, outcome: BudgetedValidationOutcome) -> "BisectRange":
        if self.midpoint is None or commit != self.midpoint:
            raise ValueError("Result does not belong to the requested midpoint")
        index = (self.good_index + self.bad_index) // 2
        if outcome is BudgetedValidationOutcome.PASSED:
            return BisectRange(self.commits, index, self.bad_index)
        if outcome is BudgetedValidationOutcome.FAILED:
            return BisectRange(self.commits, self.good_index, index)
        return self


@dataclass(frozen=True, slots=True)
class BudgetedValidationProbe:
    commit: str
    outcome: BudgetedValidationOutcome
    evidence: str
    failure_signature: str = ""

    @property
    def is_failure(self) -> bool:
        return self.outcome.is_failure

    @property
    def is_success(self) -> bool:
        return self.outcome is BudgetedValidationOutcome.PASSED

    def reproduces(self, original: "BudgetedValidationProbe") -> bool:
        return self.is_failure and self.failure_signature == original.failure_signature

    def narrows(self, original: "BudgetedValidationProbe") -> bool:
        return self.is_success or self.reproduces(original)


@dataclass(frozen=True, slots=True)
class BudgetedValidationRun:
    id: str
    started_at: datetime
    finished_at: datetime | None
    probe: BudgetedValidationProbe
    purpose: str


@dataclass(frozen=True, slots=True)
class BudgetedValidationHistory:
    suite_identity: str
    last_success: BudgetedValidationRun | None = None
    latest: BudgetedValidationRun | None = None
    last_scheduled: BudgetedValidationRun | None = None
    runs: tuple[BudgetedValidationRun, ...] = ()
    first_bad_commit: str | None = None
    diagnosis: str = ""

    def cooling_down(self, now: datetime, cadence: ValidationCadence) -> bool:
        last = self.latest
        return bool(last and (last.finished_at is None or last.probe.outcome in {
            BudgetedValidationOutcome.UNAVAILABLE, BudgetedValidationOutcome.INCONCLUSIVE,
        }) and now < last.started_at + timedelta(hours=cadence.max_delay_hours))

    @property
    def coverage_outcome(self) -> BudgetedValidationOutcome:
        """A passing diagnosis probe cannot turn the scheduled result green."""
        scheduled = self.last_scheduled
        if scheduled is None or scheduled.finished_at is None:
            return BudgetedValidationOutcome.UNAVAILABLE
        return scheduled.probe.outcome

    def append(self, run: BudgetedValidationRun) -> "BudgetedValidationHistory":
        from dataclasses import replace

        green = self.last_success
        if run.purpose == "scheduled" and run.finished_at is not None and run.probe.outcome is BudgetedValidationOutcome.PASSED:
            green = run
        return replace(self, last_success=green, latest=run,
                       last_scheduled=run if run.purpose == "scheduled" else self.last_scheduled,
                       first_bad_commit=None if run.purpose == "scheduled" else self.first_bad_commit,
                       diagnosis="" if run.purpose == "scheduled" else self.diagnosis,
                       runs=(*self.runs[-99:], run))


@dataclass(frozen=True, slots=True)
class BudgetedValidationNotice:
    suite_name: str
    suite_identity: str
    case_id: str
    run_id: str
    failed_commit: str
    last_green_commit: str | None
    first_bad_commit: str | None
    diagnosis: str
    evidence: str


@dataclass(frozen=True, slots=True)
class BudgetedValidationReportReceipt:
    attempted_at: datetime | None = None
    issue_number: int | None = None
    next_lookup_at: datetime | None = None


def coverage_exit_code(outcomes: set[BudgetedValidationOutcome], *, inspect_only: bool, acquired: bool) -> int:
    if inspect_only:
        return 0
    if not acquired or outcomes & {BudgetedValidationOutcome.UNAVAILABLE, BudgetedValidationOutcome.INCONCLUSIVE}:
        return 75
    return 1 if BudgetedValidationOutcome.FAILED in outcomes else 0


def command_verdict(exit_code: int, timed_out: bool) -> BudgetedValidationOutcome:
    if timed_out or exit_code not in {0, 1}:
        return BudgetedValidationOutcome.UNAVAILABLE
    return BudgetedValidationOutcome.PASSED if exit_code == 0 else BudgetedValidationOutcome.FAILED


def reconcile_report_verdict(exit_code: int, declared: BudgetedValidationOutcome, failures: tuple[str, ...]) -> BudgetedValidationOutcome:
    if declared is BudgetedValidationOutcome.PASSED and (exit_code != 0 or failures):
        raise ValueError("Live verdict claims success despite a failed command or test")
    if declared is BudgetedValidationOutcome.FAILED and exit_code != 1:
        raise ValueError("Live verdict claims a regression without a test-failure exit")
    return declared
