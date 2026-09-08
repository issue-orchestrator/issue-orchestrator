"""Validation record persistence, independent of the command execution workspace."""

import json
import logging
from pathlib import Path
from typing import Optional

from ..infra.atomic_json import atomic_write_json
from ..ports.session_output import ValidationRecord

logger = logging.getLogger(__name__)


class ValidationRecordStore:
    """Reads and writes validation records to disk.

    Storage layout (simplified - one location per SHA):
        <worktree>/.issue-orchestrator/validation/<sha>.json

    This allows validation caching across gates - if agent_gate and publish_gate
    use the same command, the result can be shared.
    """

    VALIDATION_DIR = ".issue-orchestrator/validation"

    def __init__(self, worktree: Path, *, record_directory: Path | None = None):
        """Initialize store for a specific worktree.

        Args:
            worktree: Command workspace; also the default cache location
            record_directory: Explicit record storage outside the command workspace
        """
        self.worktree = worktree
        self.base_dir = (
            record_directory
            if record_directory is not None
            else worktree / self.VALIDATION_DIR
        )

    def get_record_path(self, sha: str) -> Path:
        """Get the path for a validation record (one per SHA)."""
        return self.base_dir / f"{sha}.json"

    def write(self, record: ValidationRecord) -> Path:
        """Write a validation record to disk atomically.

        Atomicity matters because two gates (agent_gate, publish_gate) may
        write the same per-SHA file concurrently in different threads, and
        readers (cache lookups, the review-exchange predicate) parse the
        file as JSON — a torn write would surface as JSONDecodeError or,
        worse, a partial-but-syntactically-valid prefix.

        Args:
            record: The validation record to write

        Returns:
            Path to the written file
        """
        path = self.get_record_path(record.head_sha)
        atomic_write_json(path, record.to_dict())
        logger.debug("Wrote validation record to %s", path)
        return path

    def read(self, sha: str) -> Optional[ValidationRecord]:
        """Read a validation record from disk.

        Args:
            sha: The HEAD SHA

        Returns:
            ValidationRecord if found, None otherwise
        """
        path = self.get_record_path(sha)

        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)
            return ValidationRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to read validation record at %s: %s", path, e)
            return None

    # Legacy methods for backwards compatibility with old suite-based paths
    def _get_legacy_record_path(self, suite: str, sha: str) -> Path:
        """Get the legacy path for a validation record (per-suite)."""
        return self.base_dir / suite / f"{sha}.json"

    def read_legacy(self, suite: str, sha: str) -> Optional[ValidationRecord]:
        """Read from legacy per-suite location for backwards compatibility."""
        path = self._get_legacy_record_path(suite, sha)

        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)
            return ValidationRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to read legacy validation record at %s: %s", path, e)
            return None
