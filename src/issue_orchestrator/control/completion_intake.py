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
from ..domain.registered_completion import RegisteredCompletion
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
                record = self._ledger.read_completion(entry.entry_id)
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
        return self.processing_context(receipt, run).record

    def processing_context(
        self, receipt: CompletionIntakeReceipt, run: SessionRunAssets
    ) -> RegisteredCompletion:
        role = self._ledger.role_for_receipt(receipt.entry_id)
        entry = self._ledger.entry_for_receipt(receipt.entry_id)
        if entry.receipt != receipt or entry.run != run:
            raise CompletionIntakeError("receipt does not bind the allocated run")
        return RegisteredCompletion(
            self._ledger.read_completion(entry.entry_id),
            role,
            normalized_completion_artifact(entry),
        )

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
        return self.processing_context(receipt, run).artifact

    def import_historical(
        self, command: HistoricalIntakeCommand
    ) -> HistoricalIntakeOutcome:
        with self._drain_lock:
            return self._historical.import_historical(command)
