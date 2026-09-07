"""Completion freshness for one allocated exchange run, including respawn attempts."""

from ..domain.completion_intake import CompletionIntakeError
from ..domain.models import CompletionOutcome, CompletionRecord
from ..domain.session_run import SessionRunAssets
from ..ports.completion_intake import CompletionIntakeRuntime


class RunCompletionExchangeIntake:
    def __init__(self, intake: CompletionIntakeRuntime, run: SessionRunAssets) -> None:
        self._intake = intake
        self._run = run
        self._before_attempt: str | None = None

    def begin_attempt(self) -> None:
        receipt = self._intake.exchange_receipt(self._run)
        self._before_attempt = receipt.entry_id if receipt is not None else None

    def completion_record(self) -> CompletionRecord:
        receipt = self._intake.exchange_receipt(self._run)
        if receipt is None or receipt.entry_id == self._before_attempt:
            raise CompletionIntakeError("missing new registered completion receipt")
        record = self._intake.read_receipt(receipt, self._run)
        if (
            record.outcome is not CompletionOutcome.COMPLETED
            or not record.requests_publication
        ):
            raise CompletionIntakeError(
                "coder receipt does not request completed publication"
            )
        self._intake.require_publication_ready(receipt, self._run)
        return record

    def close_and_drain(self) -> None:
        self._intake.receipt_for_run(self._run)
