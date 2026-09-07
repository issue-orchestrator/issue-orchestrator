"""Trusted intake output: immutable owned bytes and the exact allocation binding."""

from dataclasses import dataclass
from .completion_intake import CompletionIntakeEntry, CompletionValidationAttestation
from .issue_run_evidence import IssueRunRecord
from .models import RequestedAction


@dataclass(frozen=True, slots=True)
class PreparedCompletionEvidence:
    run: IssueRunRecord
    entry: CompletionIntakeEntry
    validation: CompletionValidationAttestation
    completion_bytes: bytes
    validation_bytes: bytes
    requested_actions: tuple[RequestedAction, ...]
