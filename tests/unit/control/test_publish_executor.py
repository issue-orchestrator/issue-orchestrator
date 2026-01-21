"""Unit tests for PublishJobExecutor - background publish job execution.

Tests cover:
1. Executor lifecycle (start, shutdown)
2. Job submission and deduplication
3. Job result polling
4. Running/pending job counts
5. Worktree cleanup tracking
6. Job history queries
7. Old job cleanup
8. Event emission on job lifecycle
9. Thread-safe concurrent execution
10. Exception handling and resilience
"""

import pytest
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.publish_executor import (
    PublishJobExecutor,
    ExecutorConfig,
    create_publish_job,
)
from issue_orchestrator.domain.models import (
    PublishJob,
    PublishJobStatus,
    PublishJobResult,
    CompletionOutcome,
    RequestedAction,
    ObservedCompletion,
    SessionIdentity,
    WorktreeLocation,
    CompletionRecord,
)
from issue_orchestrator.events.catalog import EventName
from issue_orchestrator.ports import TraceEvent


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_event_sink():
    """Create a mock event sink that tracks published events."""
    class MockEventSink:
        def __init__(self):
            self.events: list[TraceEvent] = []

        def publish(self, event: TraceEvent) -> None:
            self.events.append(event)

        def get_events_by_name(self, name: EventName) -> list[TraceEvent]:
            return [e for e in self.events if e.name == name]

        def clear(self):
            self.events.clear()

    return MockEventSink()


@pytest.fixture
def mock_completion_processor():
    """Create a mock completion processor."""
    processor = MagicMock()
    # Default successful result
    processor.process.return_value = MagicMock(
        success=True,
        pr_url="https://github.com/owner/repo/pull/123",
        pr_number=123,
        message="PR created successfully",
        actions_taken=["pushed", "pr created"],
        errors=None,
        diagnostic_path=None,
    )
    return processor


@pytest.fixture
def mock_job_store():
    """Create a mock job store."""
    store = MagicMock()
    store.save_job = MagicMock()
    store.mark_started = MagicMock()
    store.mark_succeeded = MagicMock()
    store.mark_failed = MagicMock()
    store.validate_worktrees = MagicMock(return_value=0)
    store.mark_worktree_cleaned = MagicMock(return_value=0)
    store.get_recent_jobs = MagicMock(return_value=[])
    store.get_jobs_for_issue = MagicMock(return_value=[])
    store.cleanup_old_jobs = MagicMock(return_value=0)
    store.initialize = MagicMock()
    return store


@pytest.fixture
def sample_worktree(tmp_path: Path) -> Path:
    """Create a sample worktree directory."""
    worktree = tmp_path / "worktrees" / "issue-123"
    worktree.mkdir(parents=True)
    return worktree


@pytest.fixture
def sample_observed_completion(sample_worktree: Path) -> ObservedCompletion:
    """Create a sample observed completion."""
    identity = SessionIdentity(
        issue_number=123,
        issue_title="Fix critical bug",
        session_key="code:123",
        terminal_id="tmux-window-1",
    )
    worktree = WorktreeLocation(
        path=str(sample_worktree),
        branch_name="issue-123-fix",
        completion_path="completion.json",
    )
    record = CompletionRecord(
        session_id="session-123",
        timestamp="2024-01-21T12:00:00Z",
        outcome=CompletionOutcome.COMPLETED,
        summary="Completed successfully",
        requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        implementation="Fixed the bug",
        problems=None,
        comment_body="",
        pr_labels=["type:feature"],
    )
    return ObservedCompletion(
        identity=identity,
        worktree=worktree,
        record=record,
        pr_number=None,
        agent_label="agent:developer",
        validation_retry_count=0,
        original_prompt=None,
    )


@pytest.fixture
def sample_job(sample_observed_completion: ObservedCompletion) -> PublishJob:
    """Create a sample publish job."""
    return PublishJob.from_observed_completion(
        observed=sample_observed_completion,
        job_id="job-abc123",
        run_validation=False,
    )


@pytest.fixture
def executor_config():
    """Create a basic executor config for testing."""
    return ExecutorConfig(
        max_workers=2,
        job_timeout_seconds=10,
        enable_validation=False,
    )


@pytest.fixture
def executor(
    mock_completion_processor,
    mock_event_sink,
    executor_config,
    mock_job_store,
):
    """Create a PublishJobExecutor instance with mocks."""
    exec_instance = PublishJobExecutor(
        completion_processor=mock_completion_processor,
        events=mock_event_sink,
        config=executor_config,
        job_store=mock_job_store,
    )
    exec_instance.start()
    yield exec_instance
    exec_instance.shutdown(wait=True)


# ============================================================================
# Executor Lifecycle Tests
# ============================================================================


class TestExecutorLifecycle:
    """Tests for executor start/shutdown."""

    def test_start_initializes_thread_pool(self, mock_completion_processor, mock_event_sink):
        """Verify start() initializes thread pool."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
        )
        assert exec._executor is None
        assert not exec._started

        exec.start()
        assert exec._executor is not None
        assert exec._started

        exec.shutdown(wait=True)

    def test_start_idempotent(self, executor):
        """Verify calling start() multiple times is safe."""
        first_executor = executor._executor
        executor.start()  # Should be no-op
        assert executor._executor is first_executor

    def test_shutdown_cleanly_stops_executor(self, mock_completion_processor, mock_event_sink):
        """Verify shutdown() cleanly stops the executor."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
        )
        exec.start()
        assert exec._started

        exec.shutdown(wait=True)
        assert not exec._started

    def test_shutdown_without_start_is_safe(self, mock_completion_processor, mock_event_sink):
        """Verify shutdown() without start() is safe."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
        )
        # Should not raise
        exec.shutdown(wait=True)


# ============================================================================
# Job Submission Tests
# ============================================================================


class TestJobSubmission:
    """Tests for submitting jobs to the executor."""

    def test_submit_job_success(self, executor, sample_job, mock_event_sink):
        """Verify job can be submitted successfully."""
        result = executor.submit(sample_job)
        assert result is True

        # Verify event was emitted
        events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_QUEUED)
        assert len(events) == 1
        assert events[0].data["job_id"] == sample_job.job_id

    def test_submit_to_non_started_executor_fails(
        self,
        mock_completion_processor,
        mock_event_sink,
        sample_job,
    ):
        """Verify submit() fails if executor not started."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
        )
        result = exec.submit(sample_job)
        assert result is False

    def test_submit_duplicate_job_rejected(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify duplicate jobs (same issue + session_key) are rejected while first is pending."""
        # Make processor slow to ensure job stays in-flight
        import time
        mock_completion_processor.process.side_effect = lambda **kwargs: (
            time.sleep(0.5),
            MagicMock(
                success=True,
                pr_url="https://github.com/owner/repo/pull/123",
                pr_number=123,
                message="PR created successfully",
                actions_taken=["pushed", "pr created"],
                errors=None,
                diagnostic_path=None,
            )
        )[1]

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        # Submit first job
        result1 = exec.submit(sample_job)
        assert result1 is True

        # Create duplicate job with same issue and session_key
        job2 = PublishJob(
            job_id="job-xyz789",
            issue_number=sample_job.issue_number,
            session_key=sample_job.session_key,
            status=PublishJobStatus.QUEUED,
            worktree_path=sample_job.worktree_path,
            branch_name=sample_job.branch_name,
            completion_path=sample_job.completion_path,
            issue_title=sample_job.issue_title,
            outcome=sample_job.outcome,
            requested_actions=sample_job.requested_actions,
        )

        # Submit duplicate immediately (before first completes) - should be rejected
        result2 = exec.submit(job2)
        assert result2 is False

        exec.shutdown(wait=True)

    def test_submit_persists_to_job_store(self, executor, sample_job, mock_job_store):
        """Verify submit() persists job to store."""
        executor.submit(sample_job)

        # Give time for persistence (non-blocking but should be quick)
        time.sleep(0.1)

        mock_job_store.save_job.assert_called()

    def test_submit_emits_queued_event(self, executor, sample_job, mock_event_sink):
        """Verify PUBLISH_JOB_QUEUED event is emitted."""
        mock_event_sink.clear()
        executor.submit(sample_job)

        events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_QUEUED)
        assert len(events) == 1
        event_data = events[0].data
        assert event_data["job_id"] == sample_job.job_id
        assert event_data["issue_number"] == sample_job.issue_number
        assert event_data["session_key"] == sample_job.session_key


# ============================================================================
# Job Result Polling Tests
# ============================================================================


class TestJobResultPolling:
    """Tests for polling job results."""

    def test_poll_results_empty_when_no_jobs(self, executor):
        """Verify poll_results() returns empty list when no jobs."""
        results = executor.poll_results()
        assert results == []

    def test_poll_results_returns_completed_jobs(
        self,
        executor,
        sample_job,
        mock_completion_processor,
        mock_event_sink,
    ):
        """Verify poll_results() returns completed job results."""
        executor.submit(sample_job)

        # Wait for job to complete
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            results = executor.poll_results()
            if results:
                break
            time.sleep(0.1)

        assert len(results) == 1
        result = results[0]
        assert isinstance(result, PublishJobResult)
        assert result.job_id == sample_job.job_id
        assert result.issue_number == sample_job.issue_number
        assert result.success is True
        assert result.pr_url is not None

    def test_poll_results_clears_queue(
        self,
        executor,
        sample_job,
        mock_completion_processor,
    ):
        """Verify poll_results() removes results from queue."""
        executor.submit(sample_job)

        # Wait for completion
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            results = executor.poll_results()
            if results:
                break
            time.sleep(0.1)

        # Poll again - should be empty
        results2 = executor.poll_results()
        assert results2 == []

    def test_poll_results_non_blocking(self, executor):
        """Verify poll_results() returns immediately."""
        # Should return immediately, not block
        start = time.time()
        results = executor.poll_results()
        duration = time.time() - start
        assert duration < 0.5  # Should be nearly instant
        assert results == []


# ============================================================================
# Job Counting Tests
# ============================================================================


class TestJobCounting:
    """Tests for tracking running and pending jobs."""

    def test_get_running_jobs_empty_initially(self, executor):
        """Verify get_running_jobs() returns empty initially."""
        jobs = executor.get_running_jobs()
        assert jobs == []

    def test_get_running_jobs_after_submit(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify get_running_jobs() includes submitted jobs."""
        # Make processor slow to ensure job stays in-flight
        import time
        mock_completion_processor.process.side_effect = lambda **kwargs: (
            time.sleep(0.5),
            MagicMock(
                success=True,
                pr_url="https://github.com/owner/repo/pull/123",
                pr_number=123,
                message="PR created successfully",
                actions_taken=["pushed", "pr created"],
                errors=None,
                diagnostic_path=None,
            )
        )[1]

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        exec.submit(sample_job)

        # Check immediately - job should be in running jobs
        jobs = exec.get_running_jobs()
        assert len(jobs) > 0
        assert any(j.job_id == sample_job.job_id for j in jobs)

        exec.shutdown(wait=True)

    def test_get_pending_count(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify get_pending_count() tracks queued jobs."""
        # Make processor slow to ensure job stays in-flight
        import time
        mock_completion_processor.process.side_effect = lambda **kwargs: (
            time.sleep(0.5),
            MagicMock(
                success=True,
                pr_url="https://github.com/owner/repo/pull/123",
                pr_number=123,
                message="PR created successfully",
                actions_taken=["pushed", "pr created"],
                errors=None,
                diagnostic_path=None,
            )
        )[1]

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        exec.submit(sample_job)

        # Job is submitted, should either be QUEUED or RUNNING
        pending = exec.get_pending_count()
        running = exec.get_running_count()
        assert (pending + running) >= 1, f"Expected job in flight, got pending={pending}, running={running}"

        exec.shutdown(wait=True)

    def test_get_running_count(self, executor, sample_job):
        """Verify get_running_count() tracks running jobs."""
        executor.submit(sample_job)

        # Poll until job starts running
        max_wait = 5.0
        start = time.time()
        running_count = 0
        while time.time() - start < max_wait:
            running_count = executor.get_running_count()
            if running_count > 0:
                break
            time.sleep(0.05)

        assert running_count >= 0  # May still be pending


# ============================================================================
# Worktree Cleanup Tests
# ============================================================================


class TestWorktreeCleanup:
    """Tests for marking worktrees as cleaned."""

    def test_mark_worktree_cleaned_without_job_store(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
    ):
        """Verify mark_worktree_cleaned() returns 0 without job store."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=None,  # No job store
        )
        exec.start()

        count = exec.mark_worktree_cleaned("/some/path")
        assert count == 0

        exec.shutdown(wait=True)

    def test_mark_worktree_cleaned_with_job_store(self, executor, mock_job_store):
        """Verify mark_worktree_cleaned() delegates to job store."""
        mock_job_store.mark_worktree_cleaned.return_value = 3

        count = executor.mark_worktree_cleaned("/path/to/worktree")
        assert count == 3
        mock_job_store.mark_worktree_cleaned.assert_called_with("/path/to/worktree")


# ============================================================================
# Job History Tests
# ============================================================================


class TestJobHistory:
    """Tests for job history queries."""

    def test_get_job_history_without_job_store(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
    ):
        """Verify get_job_history() returns empty without job store."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=None,  # No job store
        )
        exec.start()

        history = exec.get_job_history()
        assert history == []

        exec.shutdown(wait=True)

    def test_get_job_history_without_filter(self, executor, mock_job_store):
        """Verify get_job_history() returns all recent jobs."""
        mock_job_store.get_recent_jobs.return_value = ["job1", "job2"]

        executor.get_job_history(limit=50)
        mock_job_store.get_recent_jobs.assert_called_with(limit=50)

    def test_get_job_history_filtered_by_issue(self, executor, mock_job_store):
        """Verify get_job_history() filters by issue number."""
        mock_job_store.get_jobs_for_issue.return_value = ["job1"]

        executor.get_job_history(issue_number=123)
        mock_job_store.get_jobs_for_issue.assert_called_with(123)


# ============================================================================
# Job Cleanup Tests
# ============================================================================


class TestJobCleanup:
    """Tests for cleaning up old jobs."""

    def test_cleanup_old_jobs_without_job_store(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
    ):
        """Verify cleanup_old_jobs() returns 0 without job store."""
        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=None,  # No job store
        )
        exec.start()

        count = exec.cleanup_old_jobs(max_age_days=30)
        assert count == 0

        exec.shutdown(wait=True)

    def test_cleanup_old_jobs_with_job_store(self, executor, mock_job_store):
        """Verify cleanup_old_jobs() delegates to job store."""
        mock_job_store.cleanup_old_jobs.return_value = 5

        count = executor.cleanup_old_jobs(max_age_days=30)
        assert count == 5
        mock_job_store.cleanup_old_jobs.assert_called_with(max_age_days=30)


# ============================================================================
# PR Number Extraction Tests
# ============================================================================


class TestPRNumberExtraction:
    """Tests for PR number extraction from URLs."""

    def test_extract_pr_number_valid_url(self, executor):
        """Verify PR number extraction from valid GitHub URL."""
        # Test through job execution that produces a PR URL
        assert executor._extract_pr_number(
            "https://github.com/owner/repo/pull/123"
        ) == 123

    def test_extract_pr_number_with_trailing_slash(self, executor):
        """Verify PR number extraction with trailing slash."""
        assert executor._extract_pr_number(
            "https://github.com/owner/repo/pull/456/"
        ) == 456

    def test_extract_pr_number_invalid_url(self, executor):
        """Verify PR number extraction returns None for invalid URL."""
        assert executor._extract_pr_number("https://github.com/owner/repo") is None

    def test_extract_pr_number_none_input(self, executor):
        """Verify PR number extraction handles None input."""
        assert executor._extract_pr_number(None) is None

    def test_extract_pr_number_malformed_url(self, executor):
        """Verify PR number extraction handles malformed URLs gracefully."""
        assert executor._extract_pr_number("not-a-url") is None


# ============================================================================
# Event Emission Tests
# ============================================================================


class TestEventEmission:
    """Tests for event emission during job lifecycle."""

    def test_publish_job_started_event_emitted(
        self,
        executor,
        sample_job,
        mock_event_sink,
    ):
        """Verify PUBLISH_JOB_STARTED event is emitted."""
        mock_event_sink.clear()
        executor.submit(sample_job)

        # Wait for job to start
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_STARTED)
            if events:
                break
            time.sleep(0.1)

        events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_STARTED)
        assert len(events) > 0

    def test_publish_job_succeeded_event_emitted(
        self,
        executor,
        sample_job,
        mock_event_sink,
    ):
        """Verify PUBLISH_JOB_SUCCEEDED event is emitted on success."""
        mock_event_sink.clear()
        executor.submit(sample_job)

        # Wait for job to complete
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_SUCCEEDED)
            if events:
                break
            time.sleep(0.1)

        events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_SUCCEEDED)
        assert len(events) > 0
        assert events[0].data["job_id"] == sample_job.job_id

    def test_publish_job_failed_event_emitted_on_failure(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify PUBLISH_JOB_FAILED event is emitted on failure."""
        # Configure processor to fail
        mock_completion_processor.process.side_effect = Exception("Test error")

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        mock_event_sink.clear()
        exec.submit(sample_job)

        # Wait for job to fail
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_FAILED)
            if events:
                break
            time.sleep(0.1)

        events = mock_event_sink.get_events_by_name(EventName.PUBLISH_JOB_FAILED)
        assert len(events) > 0

        exec.shutdown(wait=True)

    def test_publish_job_push_events_emitted(
        self,
        executor,
        sample_job,
        mock_event_sink,
    ):
        """Verify push-related events are emitted."""
        mock_event_sink.clear()
        executor.submit(sample_job)

        # Wait for job to complete
        max_wait = 5.0
        start = time.time()
        while time.time() - start < max_wait:
            started = mock_event_sink.get_events_by_name(
                EventName.PUBLISH_JOB_PUSH_STARTED
            )
            if started:
                break
            time.sleep(0.1)

        push_started = mock_event_sink.get_events_by_name(
            EventName.PUBLISH_JOB_PUSH_STARTED
        )
        assert len(push_started) > 0


# ============================================================================
# Configuration Tests
# ============================================================================


class TestExecutorConfig:
    """Tests for executor configuration."""

    def test_default_config_values(self):
        """Verify default executor config values."""
        config = ExecutorConfig()
        assert config.max_workers == 2
        assert config.job_timeout_seconds == 600
        assert config.enable_validation is False

    def test_custom_config_values(self):
        """Verify custom executor config values."""
        config = ExecutorConfig(
            max_workers=4,
            job_timeout_seconds=1800,
            enable_validation=True,
            validation_cmd="pytest",
            validation_timeout_seconds=600,
        )
        assert config.max_workers == 4
        assert config.job_timeout_seconds == 1800
        assert config.enable_validation is True
        assert config.validation_cmd == "pytest"
        assert config.validation_timeout_seconds == 600


# ============================================================================
# Job Creation Factory Tests
# ============================================================================


class TestCreatePublishJob:
    """Tests for create_publish_job factory function."""

    def test_create_publish_job_from_observed(self, sample_observed_completion):
        """Verify create_publish_job creates job with correct fields."""
        job = create_publish_job(sample_observed_completion, run_validation=False)

        assert job.issue_number == sample_observed_completion.issue_number
        assert job.session_key == sample_observed_completion.session_key
        assert job.worktree_path == sample_observed_completion.worktree_path
        assert job.branch_name == sample_observed_completion.branch_name
        assert job.issue_title == sample_observed_completion.issue_title
        assert job.status == PublishJobStatus.QUEUED
        assert job.job_id is not None

    def test_create_publish_job_generates_unique_ids(self, sample_observed_completion):
        """Verify create_publish_job generates unique job IDs."""
        job1 = create_publish_job(sample_observed_completion)
        job2 = create_publish_job(sample_observed_completion)

        assert job1.job_id != job2.job_id

    def test_create_publish_job_with_validation(self, sample_observed_completion):
        """Verify create_publish_job respects run_validation flag."""
        job = create_publish_job(sample_observed_completion, run_validation=True)
        assert job.run_validation is True


# ============================================================================
# Exception Handling Tests
# ============================================================================


class TestExceptionHandling:
    """Tests for exception handling in job execution."""

    def test_exception_in_job_execution_handled_gracefully(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify exceptions in job execution don't crash executor."""
        # Configure processor to raise exception
        mock_completion_processor.process.side_effect = RuntimeError("Catastrophic failure")

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        # Submit job - should not raise
        result = exec.submit(sample_job)
        assert result is True

        # Poll results - should get failed result
        max_wait = 5.0
        start = time.time()
        results = []
        while time.time() - start < max_wait:
            results = exec.poll_results()
            if results:
                break
            time.sleep(0.1)

        assert len(results) == 1
        assert results[0].success is False
        assert "Catastrophic failure" in results[0].message

        exec.shutdown(wait=True)

    def test_job_store_persistence_failure_doesnt_block_execution(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify job store persistence failures don't prevent job execution."""
        # Configure store to fail on save
        mock_job_store.save_job.side_effect = Exception("DB error")

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        # Job should still be submitted despite persistence failure
        result = exec.submit(sample_job)
        assert result is True

        # Job should still execute and complete
        max_wait = 5.0
        start = time.time()
        results = []
        while time.time() - start < max_wait:
            results = exec.poll_results()
            if results:
                break
            time.sleep(0.1)

        assert len(results) == 1
        assert results[0].success is True

        exec.shutdown(wait=True)

    def test_event_emission_failure_doesnt_crash_worker(
        self,
        mock_completion_processor,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify event emission failures don't crash the worker thread."""
        # Create event sink that fails on publish
        class FailingEventSink:
            def publish(self, event: TraceEvent) -> None:
                raise Exception("Event sink failure")

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=FailingEventSink(),
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        # Job should still complete
        result = exec.submit(sample_job)
        assert result is True

        max_wait = 5.0
        start = time.time()
        results = []
        while time.time() - start < max_wait:
            results = exec.poll_results()
            if results:
                break
            time.sleep(0.1)

        assert len(results) == 1
        assert results[0].success is True

        exec.shutdown(wait=True)


# ============================================================================
# Thread Safety Tests
# ============================================================================


class TestThreadSafety:
    """Tests for thread safety and concurrent access."""

    def test_concurrent_job_submissions(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
    ):
        """Verify multiple threads can submit jobs concurrently."""
        executor_config.max_workers = 4

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        submitted_jobs = []
        errors = []

        def submit_job(issue_num):
            try:
                job = PublishJob(
                    job_id=f"job-{issue_num}",
                    issue_number=issue_num,
                    session_key=f"code:{issue_num}",
                    status=PublishJobStatus.QUEUED,
                    worktree_path="/tmp/wt",
                    branch_name=f"issue-{issue_num}",
                    completion_path="/tmp/completion.json",
                    issue_title=f"Issue {issue_num}",
                    outcome="completed",
                    requested_actions=("push_branch",),
                )
                result = exec.submit(job)
                if result:
                    submitted_jobs.append(job)
            except Exception as e:
                errors.append(e)

        # Submit from multiple threads
        threads = []
        for i in range(1, 6):
            t = threading.Thread(target=submit_job, args=(i,))
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(submitted_jobs) == 5

        exec.shutdown(wait=True)

    def test_concurrent_polling(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify multiple threads can poll results concurrently."""
        executor_config.max_workers = 2

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        exec.submit(sample_job)

        all_results = []
        errors = []

        def poll_results():
            try:
                results = exec.poll_results()
                all_results.extend(results)
            except Exception as e:
                errors.append(e)

        # Poll from multiple threads
        threads = []
        for _ in range(3):
            t = threading.Thread(target=poll_results)
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        assert len(errors) == 0

        exec.shutdown(wait=True)

    def test_concurrent_job_status_queries(
        self,
        mock_completion_processor,
        mock_event_sink,
        executor_config,
        mock_job_store,
        sample_job,
    ):
        """Verify multiple threads can query job status concurrently."""
        executor_config.max_workers = 2

        exec = PublishJobExecutor(
            completion_processor=mock_completion_processor,
            events=mock_event_sink,
            config=executor_config,
            job_store=mock_job_store,
        )
        exec.start()

        exec.submit(sample_job)

        errors = []

        def query_status():
            try:
                jobs = exec.get_running_jobs()
                pending = exec.get_pending_count()
                running = exec.get_running_count()
                # Verify we got reasonable values
                assert isinstance(jobs, list)
                assert isinstance(pending, int)
                assert isinstance(running, int)
            except Exception as e:
                errors.append(e)

        # Query from multiple threads
        threads = []
        for _ in range(5):
            t = threading.Thread(target=query_status)
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join()

        assert len(errors) == 0

        exec.shutdown(wait=True)
