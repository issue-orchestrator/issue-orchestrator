"""StartupManager - handles orchestrator startup sequence.

This module extracts the startup logic from orchestrator.py to keep
the orchestrator as a thin mediator.

Hook verification is handled by the launcher (pre-flight doctor checks)
before the orchestrator process starts. The startup sequence here covers
runtime initialization only:

1. Clean up stale claims (in-progress labels without sessions)
2. Clean up idle terminal sessions
3. Discover and restore running sessions
4. Recover pending reviews/reworks/triage
5. Resume issues with partial work
6. Audit and cache the queue
"""

import logging
import re
import time
from typing import TYPE_CHECKING, Callable, Optional

from ..infra.analysis import analyze_issue, IssueState
from ..infra.config import Config
from ..ports.issue import Issue

if TYPE_CHECKING:
    from ..execution.queue_cache_store import QueueCacheStore
from ..domain.models import (
    OrchestratorState,
    PendingReview,
    PendingTriageReview,
    PendingValidationRetry,
    Session,
    ORCHESTRATOR_PR_MARKER,
)
from .actions import AddLabelAction, RemoveLabelAction
from .action_applier import ActionApplier
from ..events import EventName
from ..ports import EventSink, SessionRunner, TraceEvent, RepositoryHost
from ..ports.session_runner import DiscoveredSession
from ..infra import labels
from ..infra import gh_audit
from ..infra.validation_state import has_pending_retry, read_validation_state, get_retry_prompt_path
from ..infra.repo_identity import get_repo_head_sha
from ..infra.sqlite_maintenance import enforce_pragmas_on_startup, run_backups_if_due
from .worktree_manager import get_worktree_path



logger = logging.getLogger(__name__)


class StartupManager:
    """Handles the orchestrator startup sequence.

    This class extracts startup logic from orchestrator to keep
    the orchestrator focused on runtime coordination.
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        runner: SessionRunner,
        repository_host: RepositoryHost,
        action_applier: ActionApplier,
        issue_branches_fn: Callable[[], dict[int, str]],
        session_exists_fn: Callable[[str], bool],
        restore_sessions_fn: Callable[[list[DiscoveredSession]], None],
        launch_session_fn: Callable[[Issue], Optional[Session]],
        update_queue_cache_fn: Callable[[], None],
        queue_cache_store: "QueueCacheStore | None" = None,
    ):
        """Initialize the startup manager.

        Args:
            config: Application configuration
            events: Event sink for trace events
            runner: Session runner for terminal operations
            repository_host: Repository host for GitHub operations
            action_applier: Action applier for label operations
            session_exists_fn: Callback to check if a session exists
            restore_sessions_fn: Callback to restore running sessions
            launch_session_fn: Callback to launch a new session
            update_queue_cache_fn: Callback to update the queue cache
            queue_cache_store: Persistent store for queue cache (enables warm restarts)
        """
        self.config = config
        self.events = events
        self.runner = runner
        self.repository_host = repository_host
        self._action_applier = action_applier
        self._issue_branches = issue_branches_fn
        self._session_exists = session_exists_fn
        self._restore_sessions = restore_sessions_fn
        self._launch_session = launch_session_fn
        self._update_queue_cache = update_queue_cache_fn
        self._queue_cache_store = queue_cache_store

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filtering.label if configured."""
        result = list(labels)
        if self.config.filtering.label:
            result.append(self.config.filtering.label)
        return result

    def _apply_label_actions(self, actions: list[AddLabelAction | RemoveLabelAction]) -> None:
        """Apply label actions through the ActionApplier."""
        for action in actions:
            result = self._action_applier.apply(action)
            if not result.success:
                logger.warning("[startup] Label action failed: %s", result.error)

    async def run_startup(self, state: OrchestratorState) -> None:
        """Execute the full startup sequence.

        Args:
            state: The orchestrator state to update
        """
        startup_start = time.time()
        state.startup_status = "running"

        # Emit merged configuration for debugging
        self.events.publish(TraceEvent(EventName.CONFIG_MERGED, self.config.to_event_dict()))

        # Log git commit SHA for version tracking
        commit_sha = get_repo_head_sha(self.config.repo_root)
        if commit_sha:
            logger.info("Orchestrator starting: commit=%s (%s)", commit_sha[:7], commit_sha)
        else:
            logger.warning("Orchestrator starting: commit=unknown (could not read git HEAD)")

        # Step 1: Enforce SQLite pragmas and backups
        state.startup_message = "Checking SQLite state..."
        enforce_pragmas_on_startup(self.config)
        if self.config.sqlite_backup.enforce_on_startup:
            run_backups_if_due(self.config)

        # Step 2: Clean up stale claims
        state.startup_message = "Cleaning up stale claims..."
        logger.info("Starting up - checking for stale in-progress issues...")

        # Step 3: Clean up idle terminal sessions
        state.startup_message = "Cleaning up idle terminal sessions..."
        closed_tabs = self.runner.cleanup_idle_sessions()
        if closed_tabs:
            logger.info("Closed %d idle terminal sessions", closed_tabs)
            print(f"  Closed {closed_tabs} idle terminal sessions")

        # Step 4: Discover and restore running sessions
        state.startup_message = "Discovering running sessions..."
        running = self.runner.discover_running_sessions()
        if running:
            logger.info("Found %d running sessions to restore tracking", len(running))
            print(f"  Found {len(running)} running sessions to restore tracking")
            self._restore_sessions(running)

        # Step 5: Check in-progress issues and determine action
        state.startup_message = "Scanning local branches..."
        issue_branches = self._issue_branches()

        issues_to_resume: list[tuple[Issue, str]] = []
        await self._check_in_progress_issues(state, issue_branches, issues_to_resume)

        # Step 6: Recover pending code reviews
        if self.config.code_review_agent and self.config.code_review_label:
            await self._recover_pending_reviews(state)

        # Step 7: Recover pending triage reviews
        if self.config.triage_review_agent:
            await self._recover_pending_triage(state)

        # Step 8: Recover pending validation retries (crash recovery)
        self._recover_pending_validation_retries(state, issue_branches)

        # Step 9: Recover orphaned cleanups
        self._recover_orphaned_cleanups(state)

        # Step 10: Resume issues with partial work
        await self._resume_partial_work(state, issues_to_resume)

        # Step 11: Audit and cache the queue
        state.startup_message = "Auditing queue..."
        from ..infra.audit import audit_queue, print_audit
        audit_entries = audit_queue(self.config, state, self.repository_host, issue_branches=issue_branches)
        print_audit(audit_entries)

        # Step 12: Restore + sync queue cache
        self._restore_and_sync_queue(state)

        # Mark startup complete
        state.startup_status = "complete"
        state.startup_message = ""
        elapsed = time.time() - startup_start
        logger.info("Startup complete in %.1fs", elapsed)

        self.events.publish(TraceEvent(EventName.ORCHESTRATOR_READY, {
            "filtering": {
                "label": self.config.filtering.label,
                "milestone": self.config.filtering.milestone,
                "milestones": self.config.filtering.get_milestones(),
            },
            "agents": list(self.config.agents.keys()),
            "max_concurrent": self.config.max_concurrent_sessions,
            "startup_seconds": round(elapsed, 1),
        }))

    async def _check_in_progress_issues(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
        issues_to_resume: list[tuple[Issue, str]],
    ) -> None:
        """Check all in-progress issues and determine action."""
        for agent_label in self.config.agents.keys():
            issues = self._fetch_in_progress_issues_for_agent(state, agent_label)
            for issue in issues:
                self._analyze_and_handle_issue(state, issue, issue_branches, issues_to_resume, agent_label)

    def _fetch_in_progress_issues_for_agent(self, state: OrchestratorState, agent_label: str) -> list[Issue]:
        """Fetch in-progress issues for a specific agent."""
        state.startup_message = f"Checking in-progress issues for {agent_label}..."
        api_start = time.time()

        milestones = self.config.get_filter_milestones() or [None]
        issues = []
        for milestone in milestones:
            with gh_audit.context(reason=gh_audit.AuditReason.STARTUP_REFRESH, scope=gh_audit.AuditScope.STARTUP):
                issues.extend(self.repository_host.list_issues(
                    labels=self._build_labels(agent_label, self.config.get_label_in_progress()),
                    milestone=milestone, limit=self.config.filtering.fetch_limit,
                ))

        elapsed = time.time() - api_start
        logger.debug("Fetched %d in-progress issues for %s in %.1fs", len(issues), agent_label, elapsed)
        print(f"[startup] Fetched {len(issues)} in-progress issues for {agent_label} in {elapsed:.1f}s")
        return issues

    def _analyze_and_handle_issue(
        self,
        state: OrchestratorState,
        issue: Issue,
        issue_branches: dict[int, str],
        issues_to_resume: list[tuple[Issue, str]],
        agent_label: str,
    ) -> None:
        """Analyze an in-progress issue and handle appropriately."""
        state.startup_message = f"Analyzing issue #{issue.number}..."

        analysis = analyze_issue(
            issue=issue, repo=self.config.repo, issue_branches=issue_branches,
            check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
        )

        if issue.is_blocked:
            print(f"  #{issue.number}: Blocked - waiting for intervention")
        elif analysis.has_session:
            print(f"  #{issue.number}: Active session found - resuming monitoring")
        elif analysis.has_open_pr:
            self._handle_issue_with_pr(issue, analysis)
        elif analysis.has_partial_work:
            print(f"  #{issue.number}: Has branch '{analysis.branch}' with commits - queuing for resume")
            issues_to_resume.append((issue, agent_label))
        elif analysis.is_orphaned_label:
            self._clear_orphaned_label(issue)

    def _handle_issue_with_pr(self, issue: Issue, analysis: IssueState) -> None:
        """Handle issue that has an open PR."""
        if not labels.is_pr_pending(issue.labels):
            print(f"  #{issue.number}: Has open PR - adding pr-pending label (crash recovery)")
            self._apply_label_actions([
                AddLabelAction(issue_number=issue.number, label=labels.PR_PENDING, reason="startup recovery: missing pr-pending"),
                RemoveLabelAction(issue_number=issue.number, label=self.config.get_label_in_progress(), reason="startup recovery: remove stale in-progress"),
            ])
        else:
            print(f"  #{issue.number}: Has open PR ({analysis.pr_url or 'unknown'}) - already has pr-pending")

    def _clear_orphaned_label(self, issue: Issue) -> None:
        """Clear stale in-progress label from issue."""
        print(f"  #{issue.number}: No session or branch - clearing stale label")
        self._apply_label_actions([
            RemoveLabelAction(issue_number=issue.number, label=self.config.get_label_in_progress(), reason="startup recovery: clear stale in-progress"),
        ])

    async def _recover_pending_reviews(self, state: OrchestratorState) -> None:
        """Recover PRs needing code review after crash/restart."""
        state.startup_message = "Checking PRs needing code review..."
        print("\nChecking for PRs needing code review...")

        # Caller ensures code_review_label is set before calling this method
        assert self.config.code_review_label is not None
        prs = self.repository_host.get_prs_with_label(self.config.code_review_label)
        for pr in prs:
            pr_number = pr.number
            pr_url = pr.url
            pr_body = pr.body

            # Extract issue number from "Closes #N"
            issue_match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
            issue_number: int = int(issue_match.group(1)) if issue_match else pr_number

            # Check if PR was created by orchestrator
            if ORCHESTRATOR_PR_MARKER not in pr_body:
                logger.debug(f"PR #{pr_number}: Not created by orchestrator (no marker)")

            # Check if review is already in progress
            if not self._session_exists(f"review-{pr_number}"):
                review = PendingReview(
                    issue_key=self.repository_host.create_issue_key(issue_number),
                    pr_number=pr_number,
                    pr_url=pr_url,
                    branch_name=pr.branch,
                    _issue_number=issue_number,
                )
                if review not in state.pending_reviews:
                    state.pending_reviews.append(review)
                    print(f"  PR #{pr_number}: Queued for code review")
            else:
                print(f"  PR #{pr_number}: Review already in progress")

    async def _recover_pending_triage(self, state: OrchestratorState) -> None:
        """Recover pending triage review issues after crash/restart."""
        state.startup_message = "Checking for pending triage review issues..."
        print("\nChecking for pending triage review issues...")

        # Caller ensures triage_review_agent is set before calling this method
        assert self.config.triage_review_agent is not None
        triage_issues = self.repository_host.list_issues(
            labels=[self.config.triage_review_agent],
            limit=20,
        )

        for triage_issue in triage_issues:
            session_name = f"issue-{triage_issue.number}"

            if self._session_exists(session_name):
                print(f"  triage issue #{triage_issue.number}: Already running")
                continue

            if any(r.issue_number == triage_issue.number for r in state.pending_triage_reviews):
                print(f"  triage issue #{triage_issue.number}: Already queued")
                continue

            state.pending_triage_reviews.append(
                PendingTriageReview(
                    issue_number=triage_issue.number,
                    title=triage_issue.title,
                )
            )
            print(f"  triage issue #{triage_issue.number}: Queued ({triage_issue.title})")

        if state.pending_triage_reviews:
            print(f"  Found {len(state.pending_triage_reviews)} triage review(s) to process")

    def _recover_pending_validation_retries(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
    ) -> None:
        """Recover validation retries from before restart.

        Scans worktrees for issues that were mid-validation-retry when
        the orchestrator restarted. Re-queues them for immediate retry.

        Args:
            state: Orchestrator state to update
            issue_branches: Map of issue numbers to branch names
        """
        recovered = 0
        for issue_number, branch_name in issue_branches.items():
            worktree_path = get_worktree_path(self.config, issue_number)
            if not worktree_path.exists():
                continue

            # Check if this worktree has a pending validation retry
            if not has_pending_retry(worktree_path):
                continue

            # Read the validation state to get retry details
            validation_state = read_validation_state(worktree_path)
            if validation_state is None:
                continue

            # Get the retry prompt path if it exists
            retry_prompt_path = get_retry_prompt_path(worktree_path)
            retry_prompt = None
            if retry_prompt_path:
                try:
                    retry_prompt = retry_prompt_path.read_text()
                except OSError:
                    pass

            # Create a pending validation retry entry
            pending_retry = PendingValidationRetry(
                issue_number=issue_number,
                issue_title=f"Issue #{issue_number}",  # We don't have the full title here
                agent_label="",  # Will be determined when launching
                worktree_path=str(worktree_path),
                branch_name=branch_name,
                original_prompt=retry_prompt,
                validation_error=validation_state.last_error or "Unknown validation error",
                validation_error_file=validation_state.last_error_file,
                retry_count=validation_state.retry_count,
                validation_cmd=validation_state.validation_cmd,
            )
            state.pending_validation_retries.append(pending_retry)
            recovered += 1

            logger.info(
                "[startup] Recovered pending validation retry: issue=%d retry_count=%d/%d",
                issue_number,
                validation_state.retry_count,
                validation_state.max_retries,
            )

        if recovered:
            print(f"\n🔄 Recovered {recovered} pending validation retry(ies)")

    def _recover_orphaned_cleanups(self, state: OrchestratorState) -> None:
        """Recover orphaned cleanups from before restart.

        This is a simplified version - the full logic is in CleanupManager.
        """
        # Delegate to CleanupManager if needed
        pass

    def _restore_and_sync_queue(self, state: OrchestratorState) -> None:
        """Restore queue cache from SQLite and delta-sync from GitHub.

        Warm start: load cached issues + watermark from SQLite, then use
        ``list_issues_delta`` to fetch only what changed since last persist.
        Cold start: fall back to a full scan via ``_update_queue_cache``.
        Either way, persist the result back to SQLite for the next restart.
        """
        store = self._queue_cache_store
        if store is None:
            # No persistent store configured — fall back to full scan.
            state.startup_message = "Caching queue..."
            self._update_queue_cache()
            return

        state.startup_message = "Restoring queue cache..."
        cached_issues = store.load_issues(self.config.repo or "")
        cached_watermark = store.load_watermark()

        if cached_issues and cached_watermark:
            # Warm start: load from SQLite, then delta sync from GitHub
            state.startup_message = "Syncing queue changes from GitHub..."
            logger.info(
                "[STARTUP] Warm start: %d cached issues, watermark=%s",
                len(cached_issues), cached_watermark,
            )
            delta_issues, next_watermark = self.repository_host.list_issues_delta(
                since=cached_watermark, limit=200,
            )
            # Merge: start from cached, apply deltas
            issue_map: dict[int, Issue] = {i.number: i for i in cached_issues}
            for issue in delta_issues:
                if issue.state.lower() == "open":
                    issue_map[issue.number] = issue
                else:
                    issue_map.pop(issue.number, None)

            # Apply eligibility policy (scope + exclusion filters)
            from .queue_cache import QueueCache
            QueueCache(self.config, state).replace_from_refresh(list(issue_map.values()))
            state.queue_delta_watermark = next_watermark or cached_watermark
            logger.info(
                "[STARTUP] Delta sync: %d delta issues, %d in queue after filter",
                len(delta_issues), len(state.cached_queue_issues),
            )
        else:
            # Cold start (first run or empty store): full scan via existing path
            state.startup_message = "Caching queue..."
            logger.info("[STARTUP] Cold start: running full queue scan")
            self._update_queue_cache()

        # Persist updated state to SQLite for next restart
        state.startup_message = "Persisting queue cache..."
        store.save_snapshot(
            state.cached_queue_issues,
            state.queue_delta_watermark,
            repo=self.config.repo or "",
        )

    async def _resume_partial_work(
        self,
        state: OrchestratorState,
        issues_to_resume: list[tuple[Issue, str]],
    ) -> None:
        """Resume issues that have partial work (branch with commits but no session)."""
        if not issues_to_resume:
            return

        state.startup_message = f"Resuming {len(issues_to_resume)} in-progress issue(s)..."
        print(f"\n🔄 Resuming {len(issues_to_resume)} in-progress issue(s) with partial work...")

        for issue, _agent_label in issues_to_resume:
            # Check capacity
            if len(state.active_sessions) >= self.config.max_concurrent_sessions:
                print(f"  #{issue.number}: At max capacity, will resume when slot available")
                if issue.number not in state.priority_queue:
                    state.priority_queue.insert(0, issue.number)
                continue

            print(f"  #{issue.number}: Starting session to resume work...")
            session = self._launch_session(issue)
            # Note: launch_session already appends to state.active_sessions
            if session:
                print(f"  #{issue.number}: ✅ Session started")
            else:
                print(f"  #{issue.number}: ❌ Failed to start session")
