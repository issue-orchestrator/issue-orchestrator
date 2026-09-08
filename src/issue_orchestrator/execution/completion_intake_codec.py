"""Bidirectional codecs for immutable intake envelopes and their SQLite rows.

Only this adapter-level boundary knows the positional column layout. Callers
provide observed rows/custody and allocated domain assets, never expected proofs.
No connection escapes SqliteIssueRunLedger; this codec performs no I/O.
"""

import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..domain.completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeError,
    CompletionParseStatus,
    CompletionValidationAttestation,
    SubmissionOrigin,
    ValidationBinding,
)
from ..domain.session_run import SessionRunAssets, SessionRunIdentity
from .completion_intake_artifacts import canonical_bytes


def run_key(identity: SessionRunIdentity) -> str:
    return sha256(
        canonical_bytes([identity.session_name, identity.run_id, identity.started_at])
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmissionEnvelope:
    data: dict[str, Any]

    def __post_init__(self) -> None:
        expected_id = sha256(
            canonical_bytes([self.data["run_key"], self.data["submission_key"]])
        ).hexdigest()
        if self.entry_id != expected_id or self.data["kind"] != "submission":
            raise CompletionIntakeError("invalid submission envelope identity")

    @property
    def entry_id(self) -> str:
        return str(self.data["entry_id"])

    @property
    def run_key(self) -> str:
        return str(self.data["run_key"])

    @property
    def encoded(self) -> str:
        return canonical_bytes(self.data).decode()

    def require_existing(self, encoded: str) -> None:
        if encoded != self.encoded:
            raise CompletionIntakeError("ledger/envelope mismatch")

    def insert_values(self) -> tuple[object, ...]:
        data = self.data
        normalized = data["normalized_sha256"]
        return (
            self.entry_id,
            data["run_key"],
            data["submission_key"],
            data["receive_sequence"],
            data["raw_sha256"],
            data["byte_size"],
            self.entry_id + "/raw.json",
            data["parse_status"],
            data["normalization_version"],
            normalized,
            self.entry_id + "/completion.json" if normalized else None,
            self.encoded,
        )

    def verify(
        self, stored: sqlite3.Row | None, custody: dict[str, object], raw_bytes: bytes
    ) -> None:
        if custody != self.data:
            raise CompletionIntakeError("ledger/envelope mismatch")
        blobs = self.data["blobs"]
        if (
            not isinstance(blobs, dict)
            or blobs.get("raw.json") != self.data["raw_sha256"]
            or blobs.get("completion.json") != self.data["normalized_sha256"]
        ):
            raise CompletionIntakeError(
                "submission metadata does not bind artifact hashes"
            )
        if len(raw_bytes) != self.data["byte_size"]:
            raise CompletionIntakeError("submission byte size mismatch")
        if stored is None or tuple(stored) != self.insert_values():
            raise CompletionIntakeError(
                "submission ledger fields differ from custody envelope"
            )

    def decode(self, run: SessionRunAssets, root: Path) -> CompletionIntakeEntry:
        data = self.data
        normalized = data["normalized_sha256"]
        return CompletionIntakeEntry(
            self.entry_id,
            run,
            str(data["submission_key"]),
            int(str(data["receive_sequence"])),
            str(data["raw_sha256"]),
            int(str(data["byte_size"])),
            root / self.entry_id / "raw.json",
            CompletionParseStatus(str(data["parse_status"])),
            int(str(data["normalization_version"]))
            if data["normalization_version"] is not None
            else None,
            str(normalized) if normalized else None,
            root / self.entry_id / "completion.json" if normalized else None,
            SubmissionOrigin(str(data["origin"])),
            str(data["actor"]),
            str(data["received_at"]),
        )


@dataclass(frozen=True, slots=True)
class AttestationEnvelope:
    data: dict[str, Any]
    entry: CompletionIntakeEntry

    def __post_init__(self) -> None:
        if (
            self.data["entry_id"] != self.entry.entry_id
            or self.data["run_key"] != run_key(self.entry.run.identity)
            or self.data["raw_sha256"] != self.entry.raw_sha256
            or self.data["normalized_sha256"] != self.entry.normalized_sha256
        ):
            raise CompletionIntakeError("attestation does not bind completion")

    @property
    def encoded(self) -> str:
        return canonical_bytes(self.data).decode()

    def require_existing(self, encoded: str) -> None:
        if encoded != self.encoded:
            raise CompletionIntakeError("immutable validation attestation conflict")

    def insert_values(self) -> tuple[object, ...]:
        data = self.data
        return (
            self.entry.entry_id,
            run_key(self.entry.run.identity),
            self.entry.raw_sha256,
            self.entry.normalized_sha256,
            data["head_sha"],
            data["validator_digest"],
            data["result_sha256"],
            self.entry.entry_id + "-validation/validation.json",
            data["passed"],
            data["recorded_at"],
            self.encoded,
        )

    def verify(self, stored: sqlite3.Row, custody: dict[str, object]) -> None:
        if custody != self.data:
            raise CompletionIntakeError("validation ledger/envelope mismatch")
        if (
            tuple(stored) != self.insert_values()
            or self.data["blobs"].get("validation.json") != self.data["result_sha256"]
        ):
            raise CompletionIntakeError(
                "attestation ledger fields differ from custody envelope"
            )

    def decode(self, root: Path) -> CompletionValidationAttestation:
        data = self.data
        return CompletionValidationAttestation(
            ValidationBinding(
                self.entry.entry_id,
                self.entry.run.identity,
                self.entry.raw_sha256,
                str(data["normalized_sha256"]),
            ),
            data["head_sha"],
            data["validator_digest"],
            data["result_sha256"],
            root / (self.entry.entry_id + "-validation") / "validation.json",
            data["passed"],
            data["recorded_at"],
        )
