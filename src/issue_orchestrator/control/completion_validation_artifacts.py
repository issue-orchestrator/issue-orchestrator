"""Project validation records/logs into session output without granting authority."""

import json
import logging
from pathlib import Path

from ..ports.session_output import ValidationRecord
from ..domain.session_run import ValidationArtifactPaths
from ..ports.session_output import SessionOutput
from .validation import ValidationRecordStore
from .validation_record_containment import (
    copy_from_fd as _copy_from_fd,
    open_contained_validation_record as _open_contained_validation_record,
)

logger = logging.getLogger(__name__)


class CompletionValidationArtifacts:
    def __init__(self, session_output: SessionOutput) -> None:
        self.session_output = session_output

    @staticmethod
    def load(record_path: Path) -> ValidationRecord | None:
        try:
            data = json.loads(record_path.read_text())
        except OSError:
            return None
        except json.JSONDecodeError:
            return None
        try:
            return ValidationRecord.from_dict(data)
        except TypeError:
            return None

    def attach(
        self,
        worktree: Path,
        validation_artifacts: ValidationArtifactPaths,
        record: ValidationRecord | None = None,
        record_path: Path | None = None,
    ) -> None:
        """Attach validation artifacts to session output.

        Updates manifest with paths to validation files that should already exist
        in the session output directory (written directly by validation).
        """
        run_dir = validation_artifacts.run_dir
        if record_path is None and record is not None:
            record_path = ValidationRecordStore(worktree).get_record_path(
                record.head_sha
            )
        run_dir_record_path = validation_artifacts.record_path
        effective_record_path = self.materialize(
            worktree=worktree,
            record_path=record_path,
            run_dir_record_path=run_dir_record_path,
        )
        if effective_record_path is not None:
            self.session_output.update_manifest(
                run_dir,
                {"validation_record_path": str(effective_record_path)},
            )
            try:
                (run_dir / "validation-record.path").write_text(
                    str(effective_record_path)
                )
            except OSError:
                logger.debug("Failed to write validation pointer for %s", run_dir)

        # Update manifest with validation output paths (files written by validation)
        updates: dict[str, str] = {}
        stdout_path = validation_artifacts.stdout_path
        stderr_path = validation_artifacts.stderr_path

        if stdout_path.exists():
            updates["validation_stdout"] = str(stdout_path)
        if stderr_path.exists():
            updates["validation_stderr"] = str(stderr_path)

        if updates:
            self.session_output.update_manifest(run_dir, updates)

    @staticmethod
    def materialize(
        *,
        worktree: Path,
        record_path: Path | None,
        run_dir_record_path: Path,
    ) -> Path | None:
        """Resolve the run-dir record's authoritative content and return its path.

        Precedence: when ``record_path`` is supplied, the caller is asking
        the helper to publish that source as the run-dir's authoritative
        record. Falls back to a pre-existing run-dir file ONLY when no
        source was supplied — refusing the caller's source and silently
        publishing a stale local snapshot would be the #6017 P2 path-leak
        class in reverse. Returns ``None`` when nothing can be attached.
        """
        if record_path is None or not record_path.exists():
            return run_dir_record_path if run_dir_record_path.exists() else None
        # Source/destination identity check. ``_copy_from_fd`` opens
        # ``dst`` with ``open(dst, "wb")`` which truncates the file
        # before reading completes, so a same-file copy ends up as empty
        # JSON. When the caller already wrote the authoritative record
        # into run_dir (the common case post-PublishGate fix), there's
        # nothing to copy — just attach.
        try:
            same_file = record_path.resolve(
                strict=False
            ) == run_dir_record_path.resolve(strict=False)
        except OSError:
            same_file = False
        if same_file:
            return run_dir_record_path
        # Symlink-safe walk: opens the source under the worktree with
        # O_NOFOLLOW on every path component (#6017 re-review-4 P2),
        # never reopens by path string.
        src_fd = _open_contained_validation_record(str(record_path), worktree)
        if src_fd is not None and _copy_from_fd(src_fd, run_dir_record_path):
            return run_dir_record_path
        return None
