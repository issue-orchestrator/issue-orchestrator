"""Pure correspondence rules for immutable submission and attestation evidence.

Adapters supply observed bytes/rows; these rules do no I/O and mint no authority.
"""

import json
from .completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeError,
    CompletionParseStatus,
    OwnedValidationResult,
    ValidationBinding,
)


def require_owned_result(
    entry: CompletionIntakeEntry, result: OwnedValidationResult
) -> None:
    if (
        entry.parse_status is not CompletionParseStatus.ACCEPTED
        or entry.normalized_sha256 is None
    ):
        raise CompletionIntakeError("validator binding mismatch")
    expected = ValidationBinding(
        entry.entry_id, entry.run.identity, entry.raw_sha256, entry.normalized_sha256
    )
    if result.binding != expected:
        raise CompletionIntakeError("validator binding mismatch")
    payload = json.loads(result.result_bytes)
    if payload["head_sha"] != result.head_sha or payload["passed"] is not result.passed:
        raise CompletionIntakeError("validator result mismatch")
