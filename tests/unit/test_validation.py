"""Unit tests for the validation module."""

import json
import pytest
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

from issue_orchestrator.control.validation import (
    ValidationRecord,
    ValidationRecordStore,
    ValidationRunner,
    ValidationCache,
    PublishGate,
    PublishGateResult,
    AgentGate,
    AgentGateResult,
    VALIDATION_SCHEMA_VERSION,
)


class TestValidationRecord:
    """Tests for ValidationRecord dataclass."""

    def test_create_record(self):
        """Test creating a validation record."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        assert record.suite == "publish_gate"
        assert record.head_sha == "abc123"
        assert record.passed is True
        assert record.exit_code == 0

    def test_to_dict(self):
        """Test converting record to dictionary."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        data = record.to_dict()
        assert data["suite"] == "publish_gate"
        assert data["head_sha"] == "abc123"
        assert data["passed"] is True

    def test_from_dict(self):
        """Test creating record from dictionary."""
        data = {
            "schema_version": 1,
            "suite": "agent_gate",
            "head_sha": "def456",
            "passed": False,
            "exit_code": 1,
            "command": "npm test",
            "started_at": "2024-01-01T12:00:00",
            "ended_at": "2024-01-01T12:01:00",
            "timed_out": False,
            "stdout_path": None,
            "stderr_path": None,
        }

        record = ValidationRecord.from_dict(data)
        assert record.suite == "agent_gate"
        assert record.head_sha == "def456"
        assert record.passed is False

    def test_record_is_immutable(self):
        """Test that record is frozen/immutable."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        with pytest.raises(AttributeError):
            record.passed = False  # type: ignore


class TestValidationRecordStore:
    """Tests for ValidationRecordStore."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    def test_write_and_read_record(self, store):
        """Test writing and reading a record."""
        record = ValidationRecord(
            schema_version=1,
            suite="publish_gate",
            head_sha="abc123def456",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )

        # Write
        path = store.write(record)
        assert path.exists()
        # New path format: .issue-orchestrator/validation/{sha}.json (no suite subdir)
        assert "abc123def456.json" in str(path)

        # Read - new API uses just sha (not suite + sha)
        read_record = store.read("abc123def456")
        assert read_record is not None
        assert read_record.suite == record.suite
        assert read_record.head_sha == record.head_sha
        assert read_record.passed == record.passed

    def test_read_nonexistent_returns_none(self, store):
        """Test reading a non-existent record returns None."""
        record = store.read("nonexistent")
        assert record is None

    def test_read_corrupted_json_returns_none(self, store, temp_worktree):
        """Test reading corrupted JSON returns None."""
        # Create a corrupted file in the new path format (no suite subdir)
        path = temp_worktree / ".issue-orchestrator" / "validation"
        path.mkdir(parents=True)
        (path / "corrupted.json").write_text("not valid json {{{")

        record = store.read("corrupted")
        assert record is None

    def test_write_output_files(self, store):
        """Test writing stdout/stderr files."""
        # New API uses just sha (not suite + sha)
        stdout_path, stderr_path = store.write_output(
            "abc123",
            "stdout content",
            "stderr content",
        )

        assert stdout_path.exists()
        assert stderr_path.exists()
        assert stdout_path.read_text() == "stdout content"
        assert stderr_path.read_text() == "stderr content"


class TestValidationRunner:
    """Tests for ValidationRunner."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    @pytest.fixture
    def runner(self, store):
        """Create a runner with the store."""
        return ValidationRunner(store)

    def test_run_passing_command(self, runner):
        """Test running a passing command."""
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="echo 'hello'",
            timeout_seconds=10,
        )

        assert record.passed is True
        assert record.exit_code == 0
        assert record.timed_out is False
        assert record.suite == "publish_gate"
        assert record.head_sha == "abc123"

    def test_run_failing_command(self, runner):
        """Test running a failing command."""
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="exit 1",
            timeout_seconds=10,
        )

        assert record.passed is False
        assert record.exit_code == 1
        assert record.timed_out is False

    def test_run_timeout(self, runner):
        """Test command timeout."""
        record = runner.run(
            suite="publish_gate",
            head_sha="abc123",
            command="sleep 10",
            timeout_seconds=1,
        )

        assert record.passed is False
        assert record.timed_out is True
        assert record.exit_code == -1

    def test_run_writes_record_to_store(self, runner, store):
        """Test that running writes the record to the store."""
        record = runner.run(
            suite="agent_gate",
            head_sha="def456",
            command="echo 'test'",
            timeout_seconds=10,
        )

        # Read back from store (new API uses just sha)
        stored = store.read("def456")
        assert stored is not None
        assert stored.head_sha == record.head_sha
        assert stored.passed == record.passed


class TestValidationCache:
    """Tests for ValidationCache."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def store(self, temp_worktree):
        """Create a store for the temp worktree."""
        return ValidationRecordStore(temp_worktree)

    @pytest.fixture
    def cache(self, store):
        """Create a cache with the store."""
        return ValidationCache(store)

    def test_cache_miss_when_not_exists(self, cache):
        """Test cache miss when record doesn't exist."""
        # New API: lookup(sha, command=None)
        result = cache.lookup("nonexistent")
        assert result is None

    def test_cache_hit_when_exists(self, cache, store):
        """Test cache hit when record exists."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API: lookup(sha)
        result = cache.lookup("abc123")
        assert result is not None
        assert result.passed is True

    def test_cache_miss_when_sha_differs(self, cache, store):
        """Test cache miss when SHA differs."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # Different SHA should miss
        result = cache.lookup("different_sha")
        assert result is None

    def test_cache_miss_when_command_differs(self, cache, store):
        """Test cache miss when command filter doesn't match."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # Same SHA but different command filter should miss
        result = cache.lookup("abc123", command="make build")
        assert result is None

        # Same command should hit
        result = cache.lookup("abc123", command="make test")
        assert result is not None

    def test_cache_miss_when_schema_version_differs(self, cache, store, temp_worktree):
        """Test cache miss when schema version differs."""
        # Write a record with old schema version (in new path format)
        path = temp_worktree / ".issue-orchestrator" / "validation"
        path.mkdir(parents=True)
        old_record = {
            "schema_version": 0,  # Old version
            "suite": "publish_gate",
            "head_sha": "abc123",
            "passed": True,
            "exit_code": 0,
            "command": "make test",
            "started_at": "2024-01-01T12:00:00",
            "ended_at": "2024-01-01T12:01:00",
            "timed_out": False,
            "stdout_path": None,
            "stderr_path": None,
        }
        (path / "abc123.json").write_text(json.dumps(old_record))

        # New API uses just sha (optional command for filtering)
        result = cache.lookup("abc123")
        assert result is None

    def test_is_valid_hit_passing(self, cache, store):
        """Test is_valid_hit returns True for passing record."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=True,
            exit_code=0,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API uses just sha (optional command for filtering)
        assert cache.is_valid_hit("abc123") is True

    def test_is_valid_hit_failing(self, cache, store):
        """Test is_valid_hit returns False for failing record."""
        record = ValidationRecord(
            schema_version=VALIDATION_SCHEMA_VERSION,
            suite="publish_gate",
            head_sha="abc123",
            passed=False,
            exit_code=1,
            command="make test",
            started_at="2024-01-01T12:00:00",
            ended_at="2024-01-01T12:01:00",
        )
        store.write(record)

        # New API uses just sha
        assert cache.is_valid_hit("abc123") is False

    def test_is_valid_hit_nonexistent(self, cache):
        """Test is_valid_hit returns False when no record."""
        assert cache.is_valid_hit("nonexistent") is False


class TestPublishGate:
    """Tests for PublishGate facade."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize a git repo so we can get HEAD SHA
            subprocess.run(
                ["git", "init"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=worktree,
                capture_output=True,
            )
            # Create initial commit
            (worktree / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "."],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial"],
                cwd=worktree,
                capture_output=True,
            )
            yield worktree

    def test_gate_disabled_when_no_command(self, temp_worktree):
        """Test gate is disabled when no command is configured."""
        gate = PublishGate(temp_worktree, command=None)
        result = gate.check()

        assert result.allowed is True
        assert "disabled" in result.reason.lower()
        assert result.record is None

    def test_gate_passes_when_command_succeeds(self, temp_worktree):
        """Test gate passes when validation command succeeds."""
        gate = PublishGate(temp_worktree, command="echo 'ok'", timeout_seconds=10)
        result = gate.check()

        assert result.allowed is True
        assert result.record is not None
        assert result.record.passed is True
        assert result.cache_hit is False

    def test_gate_fails_when_command_fails(self, temp_worktree):
        """Test gate fails when validation command fails."""
        gate = PublishGate(temp_worktree, command="exit 1", timeout_seconds=10)
        result = gate.check()

        assert result.allowed is False
        assert result.record is not None
        assert result.record.passed is False
        assert "failed" in result.reason.lower()

    def test_gate_uses_cache_on_second_call(self, temp_worktree):
        """Test gate uses cache on subsequent calls."""
        gate = PublishGate(temp_worktree, command="echo 'ok'", timeout_seconds=10)

        # First call runs validation
        result1 = gate.check()
        assert result1.allowed is True
        assert result1.cache_hit is False

        # Second call should hit cache
        result2 = gate.check()
        assert result2.allowed is True
        assert result2.cache_hit is True

    def test_gate_fails_when_timeout(self, temp_worktree):
        """Test gate fails when command times out."""
        gate = PublishGate(temp_worktree, command="sleep 10", timeout_seconds=1)
        result = gate.check()

        assert result.allowed is False
        assert result.record is not None
        assert result.record.timed_out is True
        assert "timed out" in result.reason.lower()

    def test_gate_caches_failure(self, temp_worktree):
        """Test gate caches failed validation results."""
        gate = PublishGate(temp_worktree, command="exit 1", timeout_seconds=10)

        # First call fails
        result1 = gate.check()
        assert result1.allowed is False
        assert result1.cache_hit is False

        # Second call should hit cache with failure
        result2 = gate.check()
        assert result2.allowed is False
        assert result2.cache_hit is True


class TestAgentGate:
    """Tests for AgentGate facade."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize a git repo so we can get HEAD SHA
            subprocess.run(
                ["git", "init"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=worktree,
                capture_output=True,
            )
            # Create initial commit
            (worktree / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "."],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial"],
                cwd=worktree,
                capture_output=True,
            )
            yield worktree

    def test_gate_disabled_when_no_command(self, temp_worktree):
        """Test gate is disabled when no command is configured."""
        gate = AgentGate(temp_worktree, command=None)
        result = gate.run()

        assert result.passed is True
        assert "disabled" in result.reason.lower()
        assert result.record is None

    def test_gate_passes_when_command_succeeds(self, temp_worktree):
        """Test gate passes when validation command succeeds."""
        gate = AgentGate(temp_worktree, command="echo 'ok'", timeout_seconds=10)
        result = gate.run()

        assert result.passed is True
        assert result.record is not None
        assert result.record.passed is True
        assert result.record.suite == "agent_gate"

    def test_gate_fails_when_command_fails(self, temp_worktree):
        """Test gate fails when validation command fails."""
        gate = AgentGate(temp_worktree, command="exit 1", timeout_seconds=10)
        result = gate.run()

        assert result.passed is False
        assert result.record is not None
        assert result.record.passed is False
        assert "failed" in result.reason.lower()

    def test_gate_always_runs_no_cache(self, temp_worktree):
        """Test gate always runs validation (no caching)."""
        gate = AgentGate(temp_worktree, command="echo 'ok'", timeout_seconds=10)

        # First call runs validation
        result1 = gate.run()
        assert result1.passed is True
        assert result1.record is not None

        # Second call runs validation again (not cached)
        result2 = gate.run()
        assert result2.passed is True
        assert result2.record is not None
        # Records should have different timestamps
        assert result2.record.started_at != result1.record.started_at

    def test_gate_fails_when_timeout(self, temp_worktree):
        """Test gate fails when command times out."""
        gate = AgentGate(temp_worktree, command="sleep 10", timeout_seconds=1)
        result = gate.run()

        assert result.passed is False
        assert result.record is not None
        assert result.record.timed_out is True
        assert "timed out" in result.reason.lower()

    def test_gate_writes_record_to_store(self, temp_worktree):
        """Test gate writes validation record to store."""
        gate = AgentGate(temp_worktree, command="echo 'ok'", timeout_seconds=10)
        result = gate.run()

        # Verify record was written (new API uses just sha)
        store = ValidationRecordStore(temp_worktree)
        stored = store.read(result.record.head_sha)
        assert stored is not None
        assert stored.passed is True
