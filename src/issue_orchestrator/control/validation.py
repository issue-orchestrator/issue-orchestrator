"""Validation module - record format, storage, runner, and cache.

This module handles validation gates for the orchestrator:
- ValidationRecord: Dataclass for validation results
- ValidationRecordStore: Read/write validation records to disk
- ValidationRunner: Execute validation commands
- ValidationCache: Cache lookup for validation results

Storage location: .issue-orchestrator/validation/<suite>/<HEAD_SHA>.json
"""

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Schema version for validation records
VALIDATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ValidationRecord:
    """Immutable record of a validation run.

    Stored at: .issue-orchestrator/validation/<suite>/<head_sha>.json
    """

    schema_version: int
    suite: str  # "publish_gate" or "agent_gate"
    head_sha: str  # Git HEAD SHA at time of validation
    passed: bool  # True if exit_code == 0
    exit_code: int
    command: str  # Command that was run
    started_at: str  # ISO 8601 timestamp
    ended_at: str  # ISO 8601 timestamp
    timed_out: bool = False  # True if command timed out
    stdout_path: Optional[str] = None  # Path to stdout file (relative to worktree)
    stderr_path: Optional[str] = None  # Path to stderr file (relative to worktree)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ValidationRecord":
        """Create from dictionary (JSON deserialization)."""
        return cls(**data)


@dataclass
class ValidationResult:
    """Result of running a validation command."""

    exit_code: int
    passed: bool
    timed_out: bool
    stdout: str
    stderr: str
    started_at: datetime
    ended_at: datetime
    command: str


class ValidationRecordStore:
    """Reads and writes validation records to disk.

    Storage layout:
        <worktree>/.issue-orchestrator/validation/<suite>/<sha>.json
    """

    VALIDATION_DIR = ".issue-orchestrator/validation"

    def __init__(self, worktree: Path):
        """Initialize store for a specific worktree.

        Args:
            worktree: Path to the git worktree
        """
        self.worktree = worktree
        self.base_dir = worktree / self.VALIDATION_DIR

    def _get_record_path(self, suite: str, sha: str) -> Path:
        """Get the path for a validation record."""
        return self.base_dir / suite / f"{sha}.json"

    def _get_output_dir(self, suite: str, sha: str) -> Path:
        """Get the directory for stdout/stderr files."""
        return self.base_dir / suite / "output"

    def write(self, record: ValidationRecord) -> Path:
        """Write a validation record to disk.

        Args:
            record: The validation record to write

        Returns:
            Path to the written file
        """
        path = self._get_record_path(record.suite, record.head_sha)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            json.dump(record.to_dict(), f, indent=2)

        logger.debug("Wrote validation record to %s", path)
        return path

    def read(self, suite: str, sha: str) -> Optional[ValidationRecord]:
        """Read a validation record from disk.

        Args:
            suite: The validation suite name
            sha: The HEAD SHA

        Returns:
            ValidationRecord if found, None otherwise
        """
        path = self._get_record_path(suite, sha)

        if not path.exists():
            return None

        try:
            with open(path) as f:
                data = json.load(f)
            return ValidationRecord.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("Failed to read validation record at %s: %s", path, e)
            return None

    def write_output(
        self, suite: str, sha: str, stdout: str, stderr: str
    ) -> tuple[Path, Path]:
        """Write stdout/stderr to files.

        Args:
            suite: The validation suite name
            sha: The HEAD SHA
            stdout: Standard output content
            stderr: Standard error content

        Returns:
            Tuple of (stdout_path, stderr_path)
        """
        output_dir = self._get_output_dir(suite, sha)
        output_dir.mkdir(parents=True, exist_ok=True)

        stdout_path = output_dir / f"{sha}.stdout"
        stderr_path = output_dir / f"{sha}.stderr"

        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)

        return stdout_path, stderr_path


class ValidationRunner:
    """Runs validation commands and produces records."""

    def __init__(self, store: ValidationRecordStore):
        """Initialize runner with a record store.

        Args:
            store: Store for writing validation records
        """
        self.store = store

    def run(
        self,
        suite: str,
        head_sha: str,
        command: str,
        timeout_seconds: int = 1800,
        cwd: Optional[Path] = None,
    ) -> ValidationRecord:
        """Run a validation command and return a record.

        Args:
            suite: The validation suite name (e.g., "publish_gate")
            head_sha: The HEAD SHA to record
            command: The command to run
            timeout_seconds: Timeout in seconds
            cwd: Working directory (defaults to store's worktree)

        Returns:
            ValidationRecord with results
        """
        cwd = cwd or self.store.worktree
        started_at = datetime.now()

        logger.info("Running validation suite '%s': %s", suite, command)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            exit_code = result.returncode
            stdout = result.stdout
            stderr = result.stderr
            timed_out = False
        except subprocess.TimeoutExpired as e:
            exit_code = -1
            stdout = e.stdout.decode() if e.stdout else ""
            stderr = e.stderr.decode() if e.stderr else ""
            stderr += f"\n\n[TIMEOUT after {timeout_seconds}s]"
            timed_out = True
            logger.warning("Validation command timed out after %ds", timeout_seconds)

        ended_at = datetime.now()
        passed = exit_code == 0

        # Write stdout/stderr files
        stdout_path, stderr_path = self.store.write_output(
            suite, head_sha, stdout, stderr
        )

        # Create record
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite=suite,
            head_sha=head_sha,
            passed=passed,
            exit_code=exit_code,
            command=command,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            timed_out=timed_out,
            stdout_path=str(stdout_path.relative_to(self.store.worktree)),
            stderr_path=str(stderr_path.relative_to(self.store.worktree)),
        )

        # Write record
        self.store.write(record)

        logger.info(
            "Validation suite '%s' %s (exit_code=%d)",
            suite,
            "passed" if passed else "failed",
            exit_code,
        )

        return record


class ValidationCache:
    """Cache lookup for validation results."""

    def __init__(self, store: ValidationRecordStore):
        """Initialize cache with a record store.

        Args:
            store: Store for reading validation records
        """
        self.store = store

    def lookup(self, suite: str, sha: str) -> Optional[ValidationRecord]:
        """Look up a cached validation record.

        Args:
            suite: The validation suite name
            sha: The HEAD SHA

        Returns:
            ValidationRecord if found and valid, None otherwise
        """
        record = self.store.read(suite, sha)

        if record is None:
            logger.debug("Cache miss for %s/%s", suite, sha)
            return None

        # Validate schema version
        if record.schema_version != VALIDATION_SCHEMA_VERSION:
            logger.debug(
                "Cache miss for %s/%s: schema version mismatch (%d != %d)",
                suite,
                sha,
                record.schema_version,
                VALIDATION_SCHEMA_VERSION,
            )
            return None

        logger.debug("Cache hit for %s/%s (passed=%s)", suite, sha, record.passed)
        return record

    def is_valid_hit(self, suite: str, sha: str) -> bool:
        """Check if there's a valid passing cache entry.

        Args:
            suite: The validation suite name
            sha: The HEAD SHA

        Returns:
            True if there's a passing cache entry for this suite+SHA
        """
        record = self.lookup(suite, sha)
        return record is not None and record.passed


@dataclass
class PublishGateResult:
    """Result of a publish gate check."""

    allowed: bool
    reason: str
    record: Optional[ValidationRecord] = None
    cache_hit: bool = False


class PublishGate:
    """Facade for publish gate validation.

    Combines cache lookup and runner to provide a single check method.
    Use this before allowing publish actions (push, PR creation).
    """

    SUITE_NAME = "publish_gate"

    def __init__(
        self,
        worktree: Path,
        command: Optional[str] = None,
        timeout_seconds: int = 1800,
    ):
        """Initialize publish gate for a worktree.

        Args:
            worktree: Path to the git worktree
            command: Validation command to run (None = gate disabled)
            timeout_seconds: Timeout for validation command
        """
        self.worktree = worktree
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.store = ValidationRecordStore(worktree)
        self.cache = ValidationCache(self.store)
        self.runner = ValidationRunner(self.store)

    def _get_head_sha(self) -> Optional[str]:
        """Get the current HEAD SHA."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.worktree,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.warning("Failed to get HEAD SHA: %s", e)
        return None

    def check(self) -> PublishGateResult:
        """Check if publishing is allowed.

        This method:
        1. Returns allowed=True if no command is configured (gate disabled)
        2. Gets the current HEAD SHA
        3. Checks cache for existing passing result
        4. Runs validation if no cache hit
        5. Returns the result

        Returns:
            PublishGateResult with allowed status and reason
        """
        # Gate disabled if no command
        if not self.command:
            logger.debug("Publish gate disabled (no command configured)")
            return PublishGateResult(
                allowed=True,
                reason="Publish gate disabled (no command configured)",
            )

        # Get HEAD SHA
        head_sha = self._get_head_sha()
        if not head_sha:
            return PublishGateResult(
                allowed=False,
                reason="Cannot determine HEAD SHA",
            )

        # Check cache
        cached = self.cache.lookup(self.SUITE_NAME, head_sha)
        if cached is not None:
            if cached.passed:
                logger.info("Publish gate: cache hit (passed) for %s", head_sha[:8])
                return PublishGateResult(
                    allowed=True,
                    reason=f"Cached validation passed for {head_sha[:8]}",
                    record=cached,
                    cache_hit=True,
                )
            else:
                logger.info("Publish gate: cache hit (failed) for %s", head_sha[:8])
                return PublishGateResult(
                    allowed=False,
                    reason=f"Cached validation failed for {head_sha[:8]}",
                    record=cached,
                    cache_hit=True,
                )

        # Run validation
        logger.info("Publish gate: running validation for %s", head_sha[:8])
        record = self.runner.run(
            suite=self.SUITE_NAME,
            head_sha=head_sha,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
        )

        if record.passed:
            return PublishGateResult(
                allowed=True,
                reason=f"Validation passed for {head_sha[:8]}",
                record=record,
                cache_hit=False,
            )
        else:
            reason = f"Validation failed for {head_sha[:8]} (exit_code={record.exit_code})"
            if record.timed_out:
                reason = f"Validation timed out for {head_sha[:8]}"
            return PublishGateResult(
                allowed=False,
                reason=reason,
                record=record,
                cache_hit=False,
            )
