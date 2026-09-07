"""Bounded historical custody and isolated checkout; no publication operation."""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..domain.completion_intake import CompletionIntakeError
from ..domain.historical_intake import HistoricalIntakeCommand
from ..domain.session_run import SessionRunAssets
from ..domain.historical_intake_policy import (
    require_historical_attestation,
    historical_admission_evidence,
)
from ..domain.completion_intake_policy import normalized_completion_artifact
from ..domain.validated_work_store import AdmissionOutcome
from ..ports.git import Git
from ..ports.completion_intake import CompletionIntakeLedger
from ..ports.historical_intake import ParkedEvidenceCapture
from ..domain.validated_work_escrow import EscrowArtifacts
from .completion_intake_artifacts import (
    CompletionIntakeArtifacts,
    canonical_bytes,
    read_regular,
)


class HistoricalIntakeCustody:
    def __init__(
        self,
        *,
        repo_root: Path,
        state_root: Path,
        git: Git,
        custody: ParkedEvidenceCapture,
        ledger: CompletionIntakeLedger,
    ) -> None:
        self._repo_root = repo_root
        self._state = state_root
        self._git = git
        self._custody = custody
        self._ledger = ledger
        self._candidates = CompletionIntakeArtifacts(
            state_root / "historical-intake-candidates"
        )

    def capture_candidate(self, command: HistoricalIntakeCommand) -> bytes:
        raw = read_regular(command.candidate_path, limit=2 * 1024 * 1024)
        selected = asdict(command)
        selected["candidate_path"] = str(command.candidate_path)
        # Preserve even a replaced/invalid candidate and the exact operator selection.
        key = sha256(canonical_bytes([selected, sha256(raw).hexdigest()])).hexdigest()
        self._candidates.write(key, {"selection": selected}, {"raw.json": raw})
        return raw

    def allocate(self, command: HistoricalIntakeCommand) -> Path:
        workspaces = self._state / "historical-intake-workspaces"
        workspaces.mkdir(parents=True, exist_ok=True)
        workspace = workspaces / uuid4().hex
        self._git.run(
            self._repo_root,
            [
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(self._repo_root),
                str(workspace),
            ],
        )
        self._git.run(workspace, ["checkout", "-B", command.branch_name, command.target_head_sha])
        # Independent object custody: this is not a linked worktree or shared clone.
        return workspace

    def admit_parked(
        self, command: HistoricalIntakeCommand, entry_id: str
    ) -> AdmissionOutcome:
        entry = self._ledger.entry_for_receipt(entry_id)
        validation = self._ledger.validation_for_receipt(entry_id)
        validation = require_historical_attestation(
            command,
            self._ledger.historical_command_for_receipt(entry_id),
            entry,
            validation,
        )
        artifact = normalized_completion_artifact(entry)
        completion_bytes = read_regular(artifact.path)
        validation_bytes = read_regular(validation.result_path)
        evidence = historical_admission_evidence(
            command, entry, validation, completion_bytes, validation_bytes
        )
        return self._custody.capture(
            evidence, EscrowArtifacts(artifact.path, validation.result_path, None),
            reason="Historical intake requires separate snapshot-bound recovery approval",
            failure=None,
        )


class IsolatedCompletionValidationWorkspace:
    def __init__(self, state_root: Path, git: Git) -> None:
        self._root = state_root / "completion-validation-workspaces"
        self._git = git

    def checkout(self, run: "SessionRunAssets", head_sha: str, entry_id: str) -> Path:
        from ..domain.validated_work import require_sha

        require_sha(head_sha)
        require_sha(entry_id, size=64)
        self._root.mkdir(parents=True, exist_ok=True)
        workspace = self._root / (entry_id + "-" + uuid4().hex)
        if workspace.is_relative_to(run.worktree_path):
            raise CompletionIntakeError(
                "validator checkout must be outside agent worktrees"
            )
        self._git.run(
            run.worktree_path,
            [
                "clone",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(run.worktree_path),
                str(workspace),
            ],
        )
        self._git.run(workspace, ["checkout", "--detach", head_sha])
        return workspace
