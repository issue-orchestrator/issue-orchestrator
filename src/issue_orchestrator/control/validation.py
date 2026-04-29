"""Validation module - record format, storage, runner, and cache.

This module handles validation gates for the orchestrator:
- ValidationRecord: Dataclass for validation results (imported from ports)
- ValidationRecordStore: Read/write validation records to disk
- ValidationRunner: Execute validation commands
- ValidationCache: Cache lookup for validation results

Storage location: .issue-orchestrator/validation/<suite>/<HEAD_SHA>.json
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..infra.atomic_json import atomic_write_json
from ..infra.emit import emit_event
from ..ports import CommandRunner, CommandResult, WorkingCopy
from ..ports.session_output import ValidationRecord
from .isolation import build_runtime_tool_env

logger = logging.getLogger(__name__)

# Schema version for validation records
VALIDATION_SCHEMA_VERSION = 1


def _is_session_run_dir(path: Path, worktree: Path) -> bool:
    """Return True when path is under .issue-orchestrator/sessions/ in this worktree."""
    try:
        rel = path.resolve().relative_to(worktree.resolve())
    except ValueError:
        return False
    parts = rel.parts
    return len(parts) >= 3 and parts[0] == ".issue-orchestrator" and parts[1] == "sessions"


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

    Storage layout (simplified - one location per SHA):
        <worktree>/.issue-orchestrator/validation/<sha>.json

    This allows validation caching across gates - if agent_gate and publish_gate
    use the same command, the result can be shared.
    """

    VALIDATION_DIR = ".issue-orchestrator/validation"

    def __init__(self, worktree: Path):
        """Initialize store for a specific worktree.

        Args:
            worktree: Path to the git worktree
        """
        self.worktree = worktree
        self.base_dir = worktree / self.VALIDATION_DIR

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


class ValidationRunner:
    """Runs validation commands and produces records."""

    def __init__(self, store: ValidationRecordStore, command_runner: CommandRunner):
        """Initialize runner with a record store.

        Args:
            store: Store for writing validation records
            command_runner: Adapter for running commands
        """
        self.store = store
        self.command_runner = command_runner

    def run(
        self,
        suite: str,
        head_sha: str,
        command: str,
        timeout_seconds: int = 1800,
        cwd: Optional[Path] = None,
        session_output_dir: Optional[Path] = None,
    ) -> ValidationRecord:
        """Run a validation command and return a record.

        Args:
            suite: The validation suite name (e.g., "publish_gate")
            head_sha: The HEAD SHA to record
            command: The command to run
            timeout_seconds: Timeout in seconds
            cwd: Working directory (defaults to store's worktree)
            session_output_dir: Directory to write stdout/stderr (required)

        Returns:
            ValidationRecord with results

        Raises:
            ValueError: If session_output_dir is not provided
        """
        if session_output_dir is None:
            raise ValueError("session_output_dir is required")
        cwd = cwd or self.store.worktree
        started_at = datetime.now()

        logger.info("Running validation suite '%s': %s", suite, command)

        # Emit validation started event
        emit_event("validation.started", {
            "suite": suite,
            "sha": head_sha,
            "command": command,
            "timeout_seconds": timeout_seconds,
        })

        try:
            result = self.command_runner.run(
                command,
                shell=True,
                cwd=cwd,
                env=build_runtime_tool_env(self.store.worktree),
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            logger.exception("Validation command runner failed")
            result = CommandResult(
                returncode=-1,
                stdout="",
                stderr=f"Validation runner error: {exc}",
                timed_out=False,
            )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        timed_out = result.timed_out
        if timed_out:
            stderr += f"\n\n[TIMEOUT after {timeout_seconds}s]"
            logger.warning("Validation command timed out after %ds", timeout_seconds)

        ended_at = datetime.now()
        passed = exit_code == 0

        # Write stdout/stderr files to session output dir
        session_output_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = session_output_dir / "validation-stdout.log"
        stderr_path = session_output_dir / "validation-stderr.log"
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        logger.debug("Wrote validation output to session dir: %s", session_output_dir)

        # Store paths - relative to worktree if possible, otherwise absolute
        # (prepush_check uses a temp dir outside the worktree)
        try:
            stdout_path_str = str(stdout_path.relative_to(self.store.worktree))
            stderr_path_str = str(stderr_path.relative_to(self.store.worktree))
        except ValueError:
            # Output dir is not under worktree (e.g., prepush temp dir)
            stdout_path_str = str(stdout_path)
            stderr_path_str = str(stderr_path)

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
            stdout_path=stdout_path_str,
            stderr_path=stderr_path_str,
        )

        # Write record
        self.store.write(record)
        # Persist run-scoped validation record only for real session run dirs.
        if _is_session_run_dir(session_output_dir, self.store.worktree):
            atomic_write_json(
                session_output_dir / "validation-record.json",
                record.to_dict(),
            )

        logger.info(
            "Validation suite '%s' %s (exit_code=%d)",
            suite,
            "passed" if passed else "failed",
            exit_code,
        )

        # Emit validation completed event
        duration_seconds = (ended_at - started_at).total_seconds()
        emit_event("validation.completed", {
            "suite": suite,
            "sha": head_sha,
            "passed": passed,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": duration_seconds,
        })

        return record


class ValidationCache:
    """Cache lookup for validation results.

    The cache is now command-aware: a cached result is valid if it's for
    the same SHA AND the same command. This allows agent_gate and publish_gate
    to share validation results when they use the same command.
    """

    def __init__(self, store: ValidationRecordStore):
        """Initialize cache with a record store.

        Args:
            store: Store for reading validation records
        """
        self.store = store

    def lookup(self, sha: str, command: Optional[str] = None) -> Optional[ValidationRecord]:
        """Look up a cached validation record.

        Args:
            sha: The HEAD SHA
            command: If provided, only return record if command matches

        Returns:
            ValidationRecord if found and valid, None otherwise
        """
        record = self.store.read(sha)

        if record is None:
            logger.debug("Cache miss for %s", sha)
            emit_event("validation.cache_miss", {
                "sha": sha,
            })
            return None

        # Validate schema version
        if record.schema_version != VALIDATION_SCHEMA_VERSION:
            logger.debug(
                "Cache miss for %s: schema version mismatch (%d != %d)",
                sha,
                record.schema_version,
                VALIDATION_SCHEMA_VERSION,
            )
            emit_event("validation.cache_miss", {
                "sha": sha,
                "reason": "schema_version_mismatch",
            })
            return None

        # If command specified, check it matches
        if command and record.command != command:
            logger.debug(
                "Cache miss for %s: command mismatch (cached='%s', requested='%s')",
                sha,
                record.command,
                command,
            )
            emit_event("validation.cache_miss", {
                "sha": sha,
                "reason": "command_mismatch",
            })
            return None

        logger.debug("Cache hit for %s (passed=%s)", sha, record.passed)
        emit_event("validation.cache_hit", {
            "sha": sha,
            "passed": record.passed,
            "command": record.command,
        })
        return record

    def is_valid_hit(self, sha: str, command: Optional[str] = None) -> bool:
        """Check if there's a valid passing cache entry.

        Args:
            sha: The HEAD SHA
            command: If provided, only match if command is the same

        Returns:
            True if there's a passing cache entry for this SHA (and command)
        """
        record = self.lookup(sha, command)
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
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
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
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.store = ValidationRecordStore(worktree)
        self.cache = ValidationCache(self.store)
        self.runner = ValidationRunner(self.store, command_runner)

    def _get_head_sha(self) -> Optional[str]:
        """Get the current HEAD SHA."""
        head_sha = self.working_copy.get_head_sha(self.worktree)
        if not head_sha:
            logger.warning("Failed to get HEAD SHA in %s", self.worktree)
        return head_sha

    def check(self, session_output_dir: Optional[Path] = None) -> PublishGateResult:
        """Check if publishing is allowed.

        This method:
        1. Returns allowed=True if no command is configured (gate disabled)
        2. Gets the current HEAD SHA
        3. Checks cache for existing passing result
        4. Runs validation if no cache hit
        5. Returns the result

        Args:
            session_output_dir: If provided, write validation output directly here
                instead of validation/output/. Keeps all session artifacts together.

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

        # Check cache - only trust cached passes, not failures
        # Failures might be due to flaky tests or transient issues, so always re-run
        cached = self.cache.lookup(head_sha, self.command)
        if cached is not None and cached.passed:
            logger.info("Publish gate: cache hit (passed) for %s", head_sha[:8])
            # Materialize the cached record into the session run dir so
            # downstream consumers (manifest, review-exchange predicate, UI)
            # see the gate's authoritative result. Without this, a stale
            # ``validation-record.json`` from an earlier inline run remains
            # in place and silently contradicts the cache hit.
            if session_output_dir is not None and _is_session_run_dir(
                session_output_dir, self.store.worktree
            ):
                atomic_write_json(
                    session_output_dir / "validation-record.json",
                    cached.to_dict(),
                )
            return PublishGateResult(
                allowed=True,
                reason=f"Cached validation passed for {head_sha[:8]}",
                record=cached,
                cache_hit=True,
            )
        elif cached is not None:
            # Cached failure - log it but re-run validation
            logger.info("Publish gate: cached failure for %s, re-running validation", head_sha[:8])

        # Run validation
        logger.info("Publish gate: running validation for %s", head_sha[:8])
        record = self.runner.run(
            suite=self.SUITE_NAME,
            head_sha=head_sha,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            session_output_dir=session_output_dir,
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


@dataclass
class AgentGateResult:
    """Result of an agent gate check."""

    passed: bool
    reason: str
    record: Optional[ValidationRecord] = None
    record_path: Optional[str] = None  # Path where validation record was written


class AgentGate:
    """Validation gate for agent completion.

    Unlike PublishGate, this runs unconditionally (no cache) and
    records the result for informational purposes.
    """

    SUITE_NAME = "agent_gate"

    def __init__(
        self,
        worktree: Path,
        command_runner: CommandRunner,
        working_copy: WorkingCopy,
        command: Optional[str] = None,
        timeout_seconds: int = 1800,
    ):
        """Initialize agent gate for a worktree.

        Args:
            worktree: Path to the git worktree
            command: Validation command to run (None = gate disabled)
            timeout_seconds: Timeout for validation command
        """
        self.worktree = worktree
        self.command_runner = command_runner
        self.working_copy = working_copy
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.store = ValidationRecordStore(worktree)
        self.runner = ValidationRunner(self.store, command_runner)

    def _get_head_sha(self) -> Optional[str]:
        """Get the current HEAD SHA."""
        head_sha = self.working_copy.get_head_sha(self.worktree)
        if not head_sha:
            logger.warning("Failed to get HEAD SHA in %s", self.worktree)
        return head_sha

    def run(self, session_output_dir: Path) -> AgentGateResult:
        """Run the agent gate validation.

        Unlike PublishGate.check(), this always runs the validation
        (no cache lookup) because we want to capture the result at
        the specific point in time when the completion command is called.

        Args:
            session_output_dir: Directory to write validation output

        Returns:
            AgentGateResult with validation status
        """
        # Gate disabled if no command
        if not self.command:
            logger.debug("Agent gate disabled (no command configured)")
            return AgentGateResult(
                passed=True,
                reason="Agent gate disabled (no command configured)",
            )

        # Get HEAD SHA
        head_sha = self._get_head_sha()
        if not head_sha:
            return AgentGateResult(
                passed=False,
                reason="Cannot determine HEAD SHA",
            )

        # Run validation
        logger.info("Agent gate: running validation for %s", head_sha[:8])
        record = self.runner.run(
            suite=self.SUITE_NAME,
            head_sha=head_sha,
            command=self.command,
            timeout_seconds=self.timeout_seconds,
            session_output_dir=session_output_dir,
        )

        # Get the path where the record was written
        record_path = str(self.store.get_record_path(head_sha))

        if record.passed:
            return AgentGateResult(
                passed=True,
                reason=f"Validation passed for {head_sha[:8]}",
                record=record,
                record_path=record_path,
            )
        else:
            reason = f"Validation failed for {head_sha[:8]} (exit_code={record.exit_code})"
            if record.timed_out:
                reason = f"Validation timed out for {head_sha[:8]}"
            return AgentGateResult(
                passed=False,
                reason=reason,
                record=record,
                record_path=record_path,
            )
