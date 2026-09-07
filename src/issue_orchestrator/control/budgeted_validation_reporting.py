"""Idempotent regression handoff, observed then planned then applied by IO."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib

from ..domain.budgeted_validation import (
    BudgetedValidationHistory, BudgetedValidationNotice, BudgetedValidationSuite,
)
from ..ports.budgeted_validation import BudgetedValidationJournal, BudgetedValidationStore
from ..ports.repository_host import RepositoryHost
from .action_base import Action, ActionType


@dataclass(frozen=True, kw_only=True)
class ReportBudgetedValidationAction(Action):
    notice: BudgetedValidationNotice
    action_type: ActionType = field(default=ActionType.REPORT_BUDGETED_VALIDATION, init=False)


class BudgetedValidationReportOwner:
    def __init__(self, *, suites: tuple[BudgetedValidationSuite, ...], store: BudgetedValidationStore,
                 repository: RepositoryHost, clock: Callable[[], datetime]) -> None:
        self._suites = {suite.name: suite for suite in suites}
        self._store = store
        self._repository = repository
        self._clock = clock

    def pending(self) -> tuple[BudgetedValidationNotice, ...]:
        notices: list[BudgetedValidationNotice] = []
        for suite in self._suites.values():
            notice = _notice(suite, self._store.read(suite))
            if notice is None:
                continue
            receipt = self._store.read_report(notice.case_id)
            if receipt.issue_number is not None or (receipt.next_lookup_at and receipt.next_lookup_at > self._clock()):
                continue
            notices.append(notice)
        return tuple(notices)

    def publish(self, notice: BudgetedValidationNotice) -> int:
        result: list[int] = []
        def apply(journal: BudgetedValidationJournal) -> None:
            suite = self._suites[notice.suite_name]
            current = journal.read(suite)
            if _notice(suite, current) != notice:
                raise ValueError("Regression report became stale before application")
            receipt = journal.read_report(notice.case_id)
            if receipt.issue_number is not None:
                result.append(receipt.issue_number)
                return
            marker = f"<!-- io-budgeted-validation:{notice.case_id} -->"
            title = f"[validation] Restore {notice.suite_name} to green"
            now = self._clock()
            # Persist a lookup backoff before either remote request. An outage
            # must not turn every engine tick into a fresh GitHub scan.
            receipt = replace(receipt, next_lookup_at=now + timedelta(minutes=1))
            journal.write_report(notice.case_id, receipt)
            existing = self._repository.find_issue_by_marker(title=title, marker=marker, authoritative=True)
            if existing is not None:
                journal.write_report(notice.case_id, replace(receipt, issue_number=existing))
                result.append(existing)
                return
            if receipt.attempted_at is not None:
                raise RuntimeError("Regression issue creation is unresolved; awaiting marker reconciliation, not issuing a duplicate POST")
            journal.write_report(notice.case_id, replace(receipt, attempted_at=now))
            created = self._repository.create_issue(title=title, body=_issue_body(notice, marker), labels=[suite.issue_agent_label])
            if not created or not isinstance(created.get("number"), int):
                raise RuntimeError("Regression issue creation had no confirmed receipt")
            number = created["number"]
            journal.write_report(notice.case_id, replace(receipt, attempted_at=now, issue_number=number))
            result.append(number)
        if not self._store.run_exclusive(apply):
            raise RuntimeError("Budgeted validation is still running; report remains pending")
        return result[0]


def _notice(suite: BudgetedValidationSuite, history: BudgetedValidationHistory) -> BudgetedValidationNotice | None:
    regression = history.regression
    if not suite.enabled or regression is None:
        return None
    failed = regression.failed
    green_commit = regression.last_green_commit
    case_id = hashlib.sha256(f"{history.suite_identity}:{green_commit}:{failed.probe.failure_signature}".encode()).hexdigest()
    return BudgetedValidationNotice(
        suite.name, history.suite_identity, case_id, failed.id, failed.probe.commit,
        green_commit, regression.first_bad_commit, regression.diagnosis, failed.probe.evidence,
    )


def _issue_body(notice: BudgetedValidationNotice, marker: str) -> str:
    return (
        f"{marker}\n\nThe budgeted validation suite `{notice.suite_name}` failed. "
        "The tech lead owns following this regression through diagnosis, repair, review, and a successful validation run.\n\n"
        f"- Last successful commit: `{notice.last_green_commit or 'no baseline'}`\n"
        f"- First observed failing commit: `{notice.failed_commit}`\n"
        f"- First failing integration from bisection: `{notice.first_bad_commit or 'not established'}`\n"
        f"- Run evidence on the executor: `{notice.evidence}`\n\n"
        f"{notice.diagnosis}\n\n"
        "Completion requires a regression test at the cheapest reliable layer, the normal PR gate, "
        "and a successful explicit run of this configured budgeted suite on the fix. "
        "Quota exhaustion or an inconclusive bisect does not establish a code defect or successful coverage."
    )
