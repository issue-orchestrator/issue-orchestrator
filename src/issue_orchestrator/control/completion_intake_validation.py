"""Fresh configured validation of receipt-owned intent, with exact HEAD binding."""

import hashlib
import json
from pathlib import Path
from dataclasses import replace
from tempfile import TemporaryDirectory

from ..domain.completion_intake import (
    CompletionIntakeEntry,
    CompletionIntakeError,
    OwnedValidationResult,
    ValidationBinding,
)
from ..domain.validated_work import require_sha
from ..domain.completion_custody import validation_output_exceeds_limit
from ..ports.completion_intake import CompletionValidationWorkspace
from ..ports.command_runner import CommandRunner
from ..ports.working_copy import WorkingCopy
from .validation import ValidationRecordStore, ValidationRunner


class ConfiguredCompletionEvidenceValidator:
    def __init__(
        self,
        working_copy: WorkingCopy,
        command_runner: CommandRunner,
        workspace: CompletionValidationWorkspace,
        *,
        command: str | None,
        timeout_seconds: int,
    ) -> None:
        self._workspace = workspace
        self._working_copy = working_copy
        self._command_runner = command_runner
        self._command = command
        self._timeout = timeout_seconds

    def validate(self, entry: CompletionIntakeEntry) -> OwnedValidationResult:
        if not self._command or entry.normalized_sha256 is None:
            raise CompletionIntakeError(
                "configured validation and normalized intent are required"
            )
        worktree = entry.run.worktree_path
        head = self._working_copy.get_head_sha(worktree)
        if head is None:
            raise CompletionIntakeError("validation HEAD is unavailable")
        require_sha(head)
        worktree = self._workspace.checkout(entry.run, head, entry.entry_id)
        if self._working_copy.get_head_sha(
            worktree
        ) != head or self._working_copy.has_uncommitted_changes(worktree):
            raise CompletionIntakeError(
                "isolated validator checkout does not match selected commit"
            )
        result = run_owned_validation(
            self._command_runner,
            worktree=worktree,
            binding=ValidationBinding(
                entry.entry_id,
                entry.run.identity,
                entry.raw_sha256,
                entry.normalized_sha256,
            ),
            head_sha=head,
            command=self._command,
            timeout_seconds=self._timeout,
            custody_directory=entry.raw_path.parent.parent,
        )
        if self._working_copy.get_head_sha(
            worktree
        ) != head or self._working_copy.has_uncommitted_changes(worktree):
            raise CompletionIntakeError("workspace changed during validation")
        return result


def run_owned_validation(
    command_runner: CommandRunner,
    *,
    worktree: Path,
    binding: "ValidationBinding",
    head_sha: str,
    command: str,
    timeout_seconds: int,
    custody_directory: Path,
) -> "OwnedValidationResult":
    """Attest only freshly executed results; never a JSON/cache deserializer."""

    with TemporaryDirectory(prefix=".validation-", dir=custody_directory) as output:
        # The command's cwd/tool environment remains the isolated checkout.
        # All runner-produced files belong to this external staging directory;
        # writing a cache in the checkout would invalidate the cleanliness check.
        runner = ValidationRunner(
            ValidationRecordStore(worktree, record_directory=Path(output) / "records"),
            command_runner,
        )
        record = runner.run(
            suite="completion_intake",
            head_sha=head_sha,
            command=command,
            timeout_seconds=timeout_seconds,
            session_output_dir=Path(output),
        )
        stdout = (Path(output) / "validation-stdout.log").read_bytes()
        stderr = (Path(output) / "validation-stderr.log").read_bytes()
    destination = custody_directory / (binding.entry_id + "-validation")
    oversized = validation_output_exceeds_limit(stdout, stderr)
    record = replace(
        record,
        passed=record.passed and not record.timed_out and not oversized,
        stdout_path=str(destination / "stdout.log"),
        stderr_path=str(destination / "stderr.log"),
    )
    config = json.dumps(
        {
            "suite": "completion_intake",
            "command": command,
            "timeout_seconds": timeout_seconds,
            "version": 1,
        },
        sort_keys=True,
    ).encode()
    result = record.to_dict()
    if oversized:
        result["custody_failure"] = "validation output exceeds custody artifact limit; complete output retained in log parts"
    return OwnedValidationResult(
        binding=binding,
        head_sha=head_sha,
        validator_digest=hashlib.sha256(config).hexdigest(),
        result_bytes=json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ).encode(),
        stdout_bytes=stdout,
        stderr_bytes=stderr,
        passed=record.passed and not record.timed_out and not oversized,
        recorded_at=record.ended_at,
    )
