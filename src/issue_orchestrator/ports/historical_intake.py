"""Operator import uses owned candidate bytes, isolated validation and parked admission."""

from pathlib import Path
from typing import Protocol

from ..domain.completion_intake import CompletionIntakeEntry
from ..domain.historical_intake import HistoricalIntakeCommand, HistoricalIntakeOutcome
from ..domain.validated_work_store import AdmissionOutcome, EvidenceAdmission


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


class HistoricalParkedAdmission(Protocol):
    def admit(self, admission: "EvidenceAdmission") -> AdmissionOutcome: ...
