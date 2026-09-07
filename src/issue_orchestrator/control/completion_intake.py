"""Receipt-driven completion validation; one owner serializes drain and closure."""

from threading import RLock

from ..domain.completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeReceipt,
    CompletionParseStatus,
    SubmitCompletionEvidence,
    CompletionIntakeError,
    SubmissionOrigin,
    IntakeUnauthorized,
)
from ..domain.models import CompletionRecord
from ..domain.issue_run_evidence import IssueRunEvidence
from ..domain.validated_work_commands import AutomaticCaptureScope
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.completion_intake_policy import (
    latest_accepted_receipt,
    require_publication_attestation,
    normalized_completion_artifact,
)
from ..domain.completion_processing import ProcessingResult
from ..domain.historical_intake import HistoricalIntakeCommand, HistoricalIntakeOutcome
from ..ports.historical_intake import HistoricalIntakeHandler
from ..ports.background_job import BackgroundJobRunner

from ..domain.session_run import SessionRunAssets, RunContainedFile
from ..ports.completion_intake import (
    CompletionEvidenceValidator,
    CompletionIntakeLedger,
    CompletionExchangeIntake,
    CompletionReceiptProcessor,
)


class CompletionEvidenceIntakeService:
    def __init__(
        self,
        ledger: CompletionIntakeLedger,
        validator: CompletionEvidenceValidator,
        historical: HistoricalIntakeHandler,
        jobs: BackgroundJobRunner,
    ) -> None:
        self._jobs = jobs
        self._historical = historical
        self._ledger = ledger
        self._validator = validator
        self._drain_lock = RLock()

    def bind_exchange(self, run: SessionRunAssets) -> CompletionExchangeIntake:
        from .completion_exchange_intake import RunCompletionExchangeIntake

        self._ledger.submission_capability(
            run
        )  # Exact allocation and open lifetime are required.
        return RunCompletionExchangeIntake(self, run)

    def pump(self) -> None:
        """Resume durable receipts without running configured validation on the tick thread."""
        for completed in self._jobs.drain_completed():
            if completed.error is not None:
                raise CompletionIntakeError(
                    "receipt processing failed; evidence retained"
                ) from completed.error
        if self._ledger.pending_receipts():
            self._jobs.submit("completion-intake-drain", self._drain_job)

    def _drain_job(self) -> None:
        self.drain()

    def submission_capability(self, run: SessionRunAssets) -> str:
        return self._ledger.submission_capability(run)

    def submit(
        self, capability: str, command: SubmitCompletionEvidence
    ) -> CompletionIntakeReceipt:
        # No in-memory enqueue gap: the immutable row itself is the durable queue.
        return self._ledger.submit(capability, command).receipt

    def _process(self, entries: tuple[CompletionIntakeEntry, ...]) -> None:
        pending_ids = {entry.entry_id for entry in self._ledger.pending_receipts()}
        for entry in entries:
            if entry.entry_id not in pending_ids:
                continue
            if entry.origin is SubmissionOrigin.HISTORICAL_OPERATOR:
                self._historical.resume(entry)
                continue
            if entry.parse_status is CompletionParseStatus.ACCEPTED:
                record = self._ledger.read_owned_completion(entry.entry_id)
                if (
                    record.requests_publication
                    and self._ledger.validation_for_receipt(entry.entry_id) is None
                ):
                    self._ledger.attest_validation(
                        entry.entry_id, self._validator.validate(entry)
                    )
            self._ledger.mark_processed(entry.entry_id)

    def drain(self) -> tuple[CompletionIntakeEntry, ...]:
        with self._drain_lock:
            self._ledger.repair_intake()
            entries = self._ledger.pending_receipts()
            self._process(entries)
            return entries

    def close_and_drain(self, issue_number: int) -> tuple[CompletionIntakeEntry, ...]:
        with self._drain_lock:
            self._ledger.close_intake(issue_number)
            entries = self._ledger.entries_for_issue(issue_number)
            self._process(entries)
            return entries

    def prepare_receipt(self, receipt: CompletionIntakeReceipt, run: SessionRunAssets) -> PreparedCompletionEvidence:
        """Prepare exactly one receipt without closing its run lifetime."""
        return self._prepare_receipt(receipt, run, issue_number=None)

    def prepare_receipt_for_issue(self, receipt: CompletionIntakeReceipt, run: SessionRunAssets,
                                  issue_number: int) -> PreparedCompletionEvidence:
        """Bind numeric issue ownership before any receipt processing."""
        if type(issue_number) is not int or issue_number <= 0:
            raise CompletionIntakeError("receipt preparation requires a positive issue number")
        return self._prepare_receipt(receipt, run, issue_number=issue_number)

    def _prepare_receipt(self, receipt: CompletionIntakeReceipt, run: SessionRunAssets,
                         *, issue_number: int | None) -> PreparedCompletionEvidence:
        with self._drain_lock:
            entry = self._ledger.entry_for_receipt(receipt.entry_id)
            if entry.receipt != receipt or entry.run != run:
                raise CompletionIntakeError("receipt does not bind the allocated run")
            if issue_number is not None and entry not in self._ledger.entries_for_issue(issue_number):
                raise CompletionIntakeError("receipt does not bind the allocated issue")
            recorded = self._ledger.recorded_run(run)
            self._process((entry,))
            candidate = self._ledger.prepare_candidate(entry.entry_id, recorded)
            if candidate is None:
                raise CompletionIntakeError("receipt is not completed and validated publication intent")
            return candidate

    def prepare_termination(self, evidence: IssueRunEvidence, scope: AutomaticCaptureScope = AutomaticCaptureScope.ISSUE) -> tuple[PreparedCompletionEvidence, ...]:
        """Close, repair and drain once, then select all trusted exact-run candidates."""
        with self._drain_lock:
            entries = self._close_selected(evidence, scope)
            runs = {record.run.identity: record for record in evidence.runs}
            candidates: list[PreparedCompletionEvidence] = []
            for entry in entries:
                recorded = runs.get(entry.run.identity)
                if recorded is None or recorded.run != entry.run:
                    raise CompletionIntakeError("closed intake disagrees with exact issue run evidence")
                candidate = self._ledger.prepare_candidate(entry.entry_id, recorded)
                if candidate is not None:
                    candidates.append(candidate)
            return tuple(candidates)

    def _close_selected(self, evidence: IssueRunEvidence, scope: AutomaticCaptureScope) -> tuple[CompletionIntakeEntry, ...]:
        if scope is AutomaticCaptureScope.ISSUE:
            return self.close_and_drain(evidence.issue_number)
        for record in evidence.runs:
            self._ledger.recorded_run(record.run)
            self._ledger.close_run_intake(record.run)
        identities = {record.run.identity for record in evidence.runs}
        entries = tuple(entry for entry in self._ledger.entries_for_issue(evidence.issue_number)
            if entry.run.identity in identities)
        self._process(entries)
        return entries

    def resume_receipt(
        self,
        capability: str,
        receipt: CompletionIntakeReceipt,
        issue_number: int,
        issue_title: str,
        processor: CompletionReceiptProcessor,
    ) -> ProcessingResult:
        """Keep authorization and processing inside the terminal drain lifetime."""
        with self._drain_lock:
            run = self._ledger.run_for_capability(capability)
            entry = self._ledger.entry_for_receipt(receipt.entry_id)
            if (
                entry.run != run
                or entry.receipt != receipt
                or entry not in self._ledger.entries_for_issue(issue_number)
            ):
                raise IntakeUnauthorized(
                    "receipt does not bind authenticated run and issue"
                )
            self._process((entry,))
            return processor.process_registered_completion(
                receipt, run, issue_number, issue_title
            )

    def exchange_receipt(self, run: SessionRunAssets) -> CompletionIntakeReceipt | None:
        with self._drain_lock:
            candidates = self._ledger.entries_for_run(run.identity)
            if any(entry.run != run for entry in candidates):
                raise IntakeUnauthorized("exchange does not bind allocated run")
            self._process(candidates)
            return latest_accepted_receipt(candidates)

    def receipt_for_run(self, run: SessionRunAssets) -> CompletionIntakeReceipt | None:
        with self._drain_lock:
            self._ledger.close_run_intake(run)
            candidates = self._ledger.entries_for_run(run.identity)
            self._process(candidates)
            return latest_accepted_receipt(candidates)

    def read_receipt(
        self, receipt: CompletionIntakeReceipt, run: SessionRunAssets
    ) -> CompletionRecord:
        entry = self._ledger.entry_for_receipt(receipt.entry_id)
        if entry.receipt != receipt or entry.run != run:
            raise CompletionIntakeError("receipt does not bind the allocated run")
        record = self._ledger.read_completion(entry.entry_id)
        return record

    def require_publication_ready(
        self, receipt: CompletionIntakeReceipt, run: SessionRunAssets
    ) -> None:
        record = self.read_receipt(receipt, run)
        require_publication_attestation(
            record, self._ledger.validation_for_receipt(receipt.entry_id)
        )

    def completion_artifact(
        self, receipt: CompletionIntakeReceipt, run: SessionRunAssets
    ) -> RunContainedFile:
        self.read_receipt(receipt, run)
        entry = self._ledger.entry_for_receipt(receipt.entry_id)
        return normalized_completion_artifact(entry)

    def import_historical(
        self, command: HistoricalIntakeCommand
    ) -> HistoricalIntakeOutcome:
        with self._drain_lock:
            return self._historical.import_historical(command)
