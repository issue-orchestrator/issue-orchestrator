"""Publish job executor - runs completion processing in background threads.

This module is responsible for the EXECUTION phase of completion handling:
1. Accept publish jobs from the main thread (non-blocking)
2. Execute git push, PR creation, and validation in background threads
3. Return completed job results when polled

Architecture:
    Main Thread                         Background Worker Threads
    -----------                         -------------------------
    observe_completion() -> facts
    planner.plan() -> actions
    executor.submit(job)  ----------->  worker picks up job
    ...                                 git push (~110s)
    tick continues                      create PR (~2s)
    ...                                 run validation (~120s)
    executor.poll_results() <---------  job finished
    handle results

The executor uses a ThreadPoolExecutor with bounded concurrency to prevent
overwhelming git remotes and GitHub API.

Persistence:
    Jobs are persisted to SQLite via JobStore. This enables:
    - Crash recovery: Resume in-flight jobs after orchestrator restart
    - Audit trail: Historical record of all publish attempts
    - Orphan detection: Identify jobs whose worktrees no longer exist

    Jobs are tied to worktree lifecycle - if the worktree is gone, the job
    becomes historical only and cannot be resumed.
"""

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Optional, TYPE_CHECKING

from ..domain.models import (
    CompletionOutcome,
    CompletionRecord,
    ObservedCompletion,
    PublishJob,
    PublishJobResult,
    PublishJobStatus,
    RequestedAction,
)
from ..events.catalog import EventName
from ..ports import EventSink,  make_trace_event

if TYPE_CHECKING:
    from .completion_processor import CompletionProcessor, ProcessingResult
    from .job_store import JobRecord, JobStore
    from ..ports.command_runner import CommandRunner

logger = logging.getLogger(__name__)


@dataclass
class ExecutorConfig:
    """Configuration for the publish job executor."""

    max_workers: int = 2  # Max concurrent publish jobs
    job_timeout_seconds: int = 600  # 10 minutes max per job
    enable_validation: bool = False  # Whether to run validation after publish
    validation_cmd: str | None = None
    validation_timeout_seconds: int = 300


class PublishJobExecutor:
    """Executes publish jobs in background threads.

    Thread-safe: submit() and poll_results() are called from the main thread,
    while job execution happens in worker threads.

    Usage:
        executor = PublishJobExecutor(completion_processor, events, config)
        executor.start()

        # In main loop:
        executor.submit(job)  # Non-blocking
        ...
        results = executor.poll_results()  # Non-blocking
        for result in results:
            handle_result(result)

        # On shutdown:
        executor.shutdown(wait=True)
    """

    def __init__(
        self,
        completion_processor: "CompletionProcessor",
        events: EventSink,
        config: ExecutorConfig | None = None,
        command_runner: Optional["CommandRunner"] = None,
        job_store: Optional["JobStore"] = None,
    ):
        """Initialize the executor.

        Args:
            completion_processor: For executing publish actions (push, PR, etc.)
            events: For emitting job lifecycle events
            config: Executor configuration
            command_runner: For running validation commands (optional)
            job_store: For persisting jobs to SQLite (optional, enables crash recovery)
        """
        self._completion_processor = completion_processor
        self._events = events
        self._config = config or ExecutorConfig()
        self._command_runner = command_runner
        self._job_store = job_store

        # Thread pool - lazily initialized
        self._executor: ThreadPoolExecutor | None = None
        self._started = False

        # Thread-safe tracking of jobs
        self._lock = threading.Lock()
        self._running_jobs: dict[str, PublishJob] = {}  # job_id -> job
        self._futures: dict[str, Future[None]] = {}  # job_id -> future
        self._results: Queue[PublishJobResult] = Queue()  # Completed results

        # For deduplication: (issue_number, session_key) -> job_id
        self._job_keys: dict[tuple[int, str], str] = {}

    def start(self) -> None:
        """Start the executor thread pool.

        If a JobStore is configured, initializes the database and recovers
        any jobs that were in-flight when the orchestrator last stopped
        (provided their worktrees still exist).
        """
        if self._started:
            return

        logger.info(
            "[EXECUTOR] Starting publish job executor with %d workers",
            self._config.max_workers,
        )

        # Initialize job store and recover jobs
        if self._job_store is not None:
            self._job_store.initialize()
            orphaned = self._job_store.validate_worktrees()
            if orphaned > 0:
                logger.info(
                    "[EXECUTOR] Marked %d orphaned jobs as worktree_gone",
                    orphaned,
                )

            # Note: Job recovery is not automatic here - the orchestrator
            # should explicitly call recover_jobs() if it wants to resume
            # in-flight jobs. This allows the orchestrator to decide whether
            # to recover or start fresh.

        self._executor = ThreadPoolExecutor(
            max_workers=self._config.max_workers,
            thread_name_prefix="publish-worker",
        )
        self._started = True

    def shutdown(self, wait: bool = True, timeout: float | None = None) -> None:
        """Shutdown the executor.

        Args:
            wait: If True, wait for running jobs to complete
            timeout: Max time to wait for running jobs (None = forever)
        """
        if not self._started or self._executor is None:
            return

        logger.info(
            "[EXECUTOR] Shutting down executor (wait=%s, timeout=%s)",
            wait,
            timeout,
        )

        # Cancel pending (not yet started) futures
        with self._lock:
            for job_id, future in self._futures.items():
                if not future.running():
                    future.cancel()
                    logger.debug("[EXECUTOR] Cancelled job %s", job_id)

        # Shutdown thread pool
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        self._started = False
        logger.info("[EXECUTOR] Executor shutdown complete")

    def submit(self, job: PublishJob) -> bool:
        """Submit a job for background execution.

        This is non-blocking - the job is queued for a worker thread.
        If a JobStore is configured, the job is persisted before execution.

        Args:
            job: The publish job to execute

        Returns:
            True if job was submitted, False if duplicate or executor not started
        """
        if not self._started or self._executor is None:
            logger.warning(
                "[EXECUTOR] Cannot submit job %s - executor not started",
                job.job_id,
            )
            return False

        # Check for duplicate (same issue + session_key)
        job_key = (job.issue_number, job.session_key)
        with self._lock:
            if job_key in self._job_keys:
                existing_id = self._job_keys[job_key]
                logger.info(
                    "[EXECUTOR] Skipping duplicate job for issue=%d session_key=%s (existing=%s)",
                    job.issue_number,
                    job.session_key,
                    existing_id,
                )
                return False

            # Register job
            self._job_keys[job_key] = job.job_id
            self._running_jobs[job.job_id] = job

        # Persist job to SQLite (crash recovery point)
        if self._job_store is not None:
            try:
                self._job_store.save_job(job)
            except Exception as e:
                logger.error(
                    "[EXECUTOR] Failed to persist job %s: %s",
                    job.job_id,
                    e,
                )
                # Continue anyway - persistence is for recovery, not blocking

        # Submit to thread pool
        future = self._executor.submit(self._execute_job, job)
        with self._lock:
            self._futures[job.job_id] = future

        self._emit_event(EventName.PUBLISH_JOB_QUEUED, {
            "job_id": job.job_id,
            "issue_number": job.issue_number,
            "session_key": job.session_key,
            "worktree_path": job.worktree_path,
        })

        logger.info(
            "[EXECUTOR] Submitted job %s for issue=%d session_key=%s",
            job.job_id,
            job.issue_number,
            job.session_key,
        )
        return True

    def poll_results(self) -> list[PublishJobResult]:
        """Poll for completed job results.

        This is non-blocking - returns immediately with any available results.

        Returns:
            List of completed job results (may be empty)
        """
        results = []
        while True:
            try:
                result = self._results.get_nowait()
                results.append(result)
            except Empty:
                break

        if results:
            logger.debug("[EXECUTOR] Polled %d completed results", len(results))

        return results

    def get_running_jobs(self) -> list[PublishJob]:
        """Get list of currently running jobs (for status display)."""
        with self._lock:
            return list(self._running_jobs.values())

    def get_pending_count(self) -> int:
        """Get count of jobs that haven't started yet."""
        with self._lock:
            return sum(
                1 for job in self._running_jobs.values()
                if job.status == PublishJobStatus.QUEUED
            )

    def get_running_count(self) -> int:
        """Get count of jobs currently executing."""
        with self._lock:
            return sum(
                1 for job in self._running_jobs.values()
                if job.status == PublishJobStatus.RUNNING
            )

    def mark_worktree_cleaned(self, worktree_path: str) -> int:
        """Mark all jobs for a worktree as WORKTREE_GONE.

        Called when a worktree is being cleaned up. This transitions any
        non-terminal jobs to a historical state since they can no longer
        be resumed.

        Args:
            worktree_path: Path to the cleaned worktree

        Returns:
            Number of jobs marked as worktree_gone
        """
        if self._job_store is None:
            return 0

        count = self._job_store.mark_worktree_cleaned(worktree_path)
        if count > 0:
            logger.info(
                "[EXECUTOR] Marked %d jobs as worktree_gone for: %s",
                count,
                worktree_path,
            )
        return count

    def get_job_history(
        self,
        issue_number: int | None = None,
        limit: int = 100,
    ) -> "list[JobRecord]":
        """Get historical job records from the database.

        Args:
            issue_number: If provided, filter to this issue only
            limit: Maximum records to return

        Returns:
            List of JobRecord objects
        """
        if self._job_store is None:
            return []

        if issue_number is not None:
            return self._job_store.get_jobs_for_issue(issue_number)
        return self._job_store.get_recent_jobs(limit=limit)

    def cleanup_old_jobs(self, max_age_days: int = 30) -> int:
        """Delete old terminal jobs to prevent unbounded database growth.

        Args:
            max_age_days: Delete terminal jobs older than this many days

        Returns:
            Number of jobs deleted
        """
        if self._job_store is None:
            return 0

        return self._job_store.cleanup_old_jobs(max_age_days=max_age_days)

    def _execute_job(self, job: PublishJob) -> None:
        """Execute a publish job in a worker thread.

        This runs in a background thread, not the main thread.
        Job status is updated in SQLite at each stage for crash recovery.
        """
        job.mark_started()

        # Update status in database
        if self._job_store is not None:
            self._job_store.mark_started(job.job_id)

        self._emit_event(EventName.PUBLISH_JOB_STARTED, {
            "job_id": job.job_id,
            "issue_number": job.issue_number,
            "session_key": job.session_key,
            "attempt": job.attempt_count,
        })

        logger.info(
            "[WORKER] Starting job %s for issue=%d (attempt %d)",
            job.job_id,
            job.issue_number,
            job.attempt_count,
        )

        try:
            # Build a CompletionRecord from the job data
            record = self._build_completion_record(job)

            # Execute publish actions via completion processor
            result = self._run_publish_actions(job, record)

            if result.success:
                pr_number = self._extract_pr_number(result.pr_url)
                job.mark_succeeded(
                    pr_url=result.pr_url,
                    pr_number=pr_number,
                    message=result.message,
                )
                # Persist success to database
                if self._job_store is not None:
                    self._job_store.mark_succeeded(
                        job.job_id,
                        pr_url=result.pr_url,
                        pr_number=pr_number,
                    )
                logger.info(
                    "[WORKER] Job %s succeeded: pr_url=%s",
                    job.job_id,
                    result.pr_url,
                )
            else:
                errors_list = list(result.errors) if result.errors else None
                job.mark_failed(
                    error=result.message,
                    errors=errors_list,
                    diagnostic_path=result.diagnostic_path,
                )
                # Persist failure to database
                if self._job_store is not None:
                    self._job_store.mark_failed(
                        job.job_id,
                        error_message=result.message,
                        errors=errors_list,
                    )
                logger.warning(
                    "[WORKER] Job %s failed: %s",
                    job.job_id,
                    result.message,
                )

            # Build and queue result
            job_result = self._build_job_result(job, result)

        except Exception as e:
            logger.exception(
                "[WORKER] Job %s raised exception: %s",
                job.job_id,
                e,
            )
            job.mark_failed(error=str(e))
            # Persist failure to database
            if self._job_store is not None:
                self._job_store.mark_failed(job.job_id, error_message=str(e))
            job_result = PublishJobResult(
                job_id=job.job_id,
                issue_number=job.issue_number,
                session_key=job.session_key,
                success=False,
                message=str(e),
                duration_seconds=job.duration_seconds,
            )

        # Queue result for main thread to pick up
        self._results.put(job_result)

        # Emit completion event
        if job_result.success:
            self._emit_event(EventName.PUBLISH_JOB_SUCCEEDED, {
                "job_id": job.job_id,
                "issue_number": job.issue_number,
                "pr_url": job_result.pr_url,
                "duration_seconds": job_result.duration_seconds,
            })
        else:
            self._emit_event(EventName.PUBLISH_JOB_FAILED, {
                "job_id": job.job_id,
                "issue_number": job.issue_number,
                "error": job_result.message,
                "duration_seconds": job_result.duration_seconds,
            })

        # Cleanup tracking
        job_key = (job.issue_number, job.session_key)
        with self._lock:
            self._running_jobs.pop(job.job_id, None)
            self._futures.pop(job.job_id, None)
            self._job_keys.pop(job_key, None)

    def _build_completion_record(self, job: PublishJob) -> CompletionRecord:
        """Build a CompletionRecord from job data for completion processor."""
        return CompletionRecord(
            session_id=job.session_key,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            outcome=CompletionOutcome(job.outcome),
            summary=f"Job {job.job_id}",
            requested_actions=[RequestedAction(a) for a in job.requested_actions],
            implementation=job.implementation,
            problems=job.problems,
            comment_body=job.comment_body,
            pr_labels=list(job.pr_labels) if job.pr_labels else None,
        )

    def _run_publish_actions(
        self,
        job: PublishJob,
        record: CompletionRecord,
    ) -> "ProcessingResult":
        """Run publish actions via completion processor.

        This is the actual I/O-heavy work: git push, PR creation, etc.
        """
        worktree = Path(job.worktree_path)

        # Emit push started event
        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            self._emit_event(EventName.PUBLISH_JOB_PUSH_STARTED, {
                "job_id": job.job_id,
                "issue_number": job.issue_number,
            })

        # Use the existing completion processor for the actual work
        result = self._completion_processor.process(
            worktree=worktree,
            issue_number=job.issue_number,
            issue_title=job.issue_title,
            run_assets=job.run_assets,
            pr_number=job.pr_number,
            completion_path=job.completion_path,
            agent_label=job.agent_label,
        )

        # Emit push completed event
        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            pushed = result.actions_taken and any(
                "pushed" in a.lower() for a in result.actions_taken
            )
            self._emit_event(EventName.PUBLISH_JOB_PUSH_COMPLETED, {
                "job_id": job.job_id,
                "issue_number": job.issue_number,
                "success": pushed,
            })

        # Emit PR created event if applicable
        if result.pr_url:
            self._emit_event(EventName.PUBLISH_JOB_PR_CREATED, {
                "job_id": job.job_id,
                "issue_number": job.issue_number,
                "pr_url": result.pr_url,
            })

        return result

    def _build_job_result(
        self,
        job: PublishJob,
        processing_result: "ProcessingResult",
    ) -> PublishJobResult:
        """Build a PublishJobResult from job and processing result."""
        return PublishJobResult(
            job_id=job.job_id,
            issue_number=job.issue_number,
            session_key=job.session_key,
            success=processing_result.success,
            failure_kind=processing_result.failure_kind,
            pr_url=processing_result.pr_url,
            pr_number=self._extract_pr_number(processing_result.pr_url),
            message=processing_result.message,
            errors=tuple(processing_result.errors) if processing_result.errors else None,
            diagnostic_path=processing_result.diagnostic_path,
            duration_seconds=job.duration_seconds,
            review_exchange_completed=processing_result.review_exchange_completed,
            review_exchange_deferred=processing_result.review_exchange_deferred,
            retry_publish=job.retry_publish,
            issue_title=job.issue_title,
            agent_label=job.agent_label,
            worktree_path=job.worktree_path,
        )

    def _extract_pr_number(self, pr_url: str | None) -> int | None:
        """Extract PR number from URL like https://github.com/owner/repo/pull/123."""
        if not pr_url:
            return None
        try:
            # URL format: https://github.com/owner/repo/pull/123
            parts = pr_url.rstrip("/").split("/")
            if len(parts) >= 2 and parts[-2] == "pull":
                return int(parts[-1])
        except (ValueError, IndexError):
            pass
        return None

    def _emit_event(self, event_name: EventName, data: dict[str, Any]) -> None:
        """Emit a trace event (thread-safe)."""
        try:
            self._events.publish(make_trace_event(event_name, data))
        except Exception as e:
            # Don't let event emission failures crash the worker
            logger.warning(
                "[EXECUTOR] Failed to emit event %s: %s",
                event_name,
                e,
            )


def create_publish_job(
    observed: ObservedCompletion,
    run_validation: bool = False,
) -> PublishJob:
    """Factory function to create a publish job from an observed completion.

    Args:
        observed: The observed completion facts
        run_validation: Whether to run validation after publish

    Returns:
        A new PublishJob ready for submission
    """
    job_id = str(uuid.uuid4())
    return PublishJob.from_observed_completion(
        observed=observed,
        job_id=job_id,
        run_validation=run_validation,
    )
