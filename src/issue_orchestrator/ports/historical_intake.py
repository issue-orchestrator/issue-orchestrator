"""Operator import uses owned candidate bytes, isolated validation and parked admission."""

from pathlib import Path
from typing import Protocol

from ..domain.completion_intake import CompletionIntakeEntry
from ..domain.historical_intake import HistoricalIntakeCommand, HistoricalIntakeOutcome
from ..domain.validated_work_store import AdmissionOutcome, EvidenceAdmission
from ..domain.validated_work import ValidatedWorkEvidence, ValidatedWorkFailure
from ..domain.validated_work_escrow import EscrowArtifacts


class HistoricalIntakeHandler(Protocol):
    def resume(self, entry: CompletionIntakeEntry) -> HistoricalIntakeOutcome: ...
    def import_historical(
        self, command: HistoricalIntakeCommand
    ) -> HistoricalIntakeOutcome: ...


class HistoricalIntakeWorkspace(Protocol):
    def capture_candidate(self, command: HistoricalIntakeCommand) -> bytes: ...
    def allocate(self, command: HistoricalIntakeCommand) -> Path: ...
    def admit_parked(
        self, command: HistoricalIntakeCommand, entry_id: str
    ) -> AdmissionOutcome: ...


class ParkedEvidenceCapture(Protocol):
    def capture(self, evidence: ValidatedWorkEvidence, sources: EscrowArtifacts, *, reason: str, failure: ValidatedWorkFailure | None) -> AdmissionOutcome: ...


class HistoricalParkedAdmission(Protocol):
    def admit(self, admission: "EvidenceAdmission") -> AdmissionOutcome: ...
