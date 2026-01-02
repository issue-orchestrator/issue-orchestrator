"""StartupManager - handles orchestrator startup sequence.

This module extracts the startup logic from orchestrator.py to keep
the orchestrator as a thin mediator.

The startup sequence:
1. Verify AI meta-agent hooks
2. Clean up stale claims (in-progress labels without sessions)
3. Clean up idle terminal sessions
4. Discover and restore running sessions
5. Recover pending reviews/reworks/triage
6. Resume issues with partial work
7. Audit and cache the queue
"""

import logging
import re
import time
from typing import Callable, Optional

from ..infra.analysis import analyze_issue
from ..config import Config
from ..ports.issue import Issue
from ..models import (
    OrchestratorState,
    PendingReview,
    PendingTriageReview,
    Session,
    ORCHESTRATOR_PR_MARKER,
)
from ..events import EventName
from ..ports import EventSink, SessionRunner, TraceEvent, RepositoryHost, HookVerifier
from ..ports.session_runner import DiscoveredSession
from ..infra import labels
from .. import gh_audit



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
        hook_verifier: HookVerifier,
        issue_branches_fn: Callable[[], dict[int, str]],
        session_exists_fn: Callable[[str], bool],
        restore_sessions_fn: Callable[[list[DiscoveredSession]], None],
        launch_session_fn: Callable[[Issue], Optional[Session]],
        update_queue_cache_fn: Callable[[], None],
    ):
        """Initialize the startup manager.

        Args:
            config: Application configuration
            events: Event sink for trace events
            runner: Session runner for terminal operations
            repository_host: Repository host for GitHub operations
            session_exists_fn: Callback to check if a session exists
            restore_sessions_fn: Callback to restore running sessions
            launch_session_fn: Callback to launch a new session
            update_queue_cache_fn: Callback to update the queue cache
        """
        self.config = config
        self.events = events
        self.runner = runner
        self.repository_host = repository_host
        self.hook_verifier = hook_verifier
        self._issue_branches = issue_branches_fn
        self._session_exists = session_exists_fn
        self._restore_sessions = restore_sessions_fn
        self._launch_session = launch_session_fn
        self._update_queue_cache = update_queue_cache_fn

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filter_label if configured."""
        result = list(labels)
        if self.config.filter_label:
            result.append(self.config.filter_label)
        return result

    async def run_startup(self, state: OrchestratorState) -> None:
        """Execute the full startup sequence.

        Args:
            state: The orchestrator state to update
        """
        startup_start = time.time()
        state.startup_status = "running"

        # Emit merged configuration for debugging
        self.events.publish(TraceEvent(EventName.CONFIG_MERGED, self.config.to_event_dict()))

        # Step 1: Verify AI meta-agent hooks
        state.startup_message = "Verifying hook enforcement..."
        await self._verify_hooks()

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

        # Step 8: Recover orphaned cleanups
        self._recover_orphaned_cleanups(state)

        # Step 9: Resume issues with partial work
        await self._resume_partial_work(state, issues_to_resume)

        # Step 10: Audit and cache the queue
        state.startup_message = "Auditing queue..."
        from ..infra.audit import audit_queue, print_audit
        audit_entries = audit_queue(self.config, state, self.repository_host, issue_branches=issue_branches)
        print_audit(audit_entries)

        state.startup_message = "Caching queue..."
        self._update_queue_cache()

        # Mark startup complete
        state.startup_status = "complete"
        state.startup_message = ""
        elapsed = time.time() - startup_start
        logger.info("Startup complete in %.1fs", elapsed)

        self.events.publish(TraceEvent(EventName.ORCHESTRATOR_READY, {
            "filter_label": self.config.filter_label,
            "filter_milestone": self.config.filter_milestone,
            "filter_milestones": self.config.get_filter_milestones(),
            "agents": list(self.config.agents.keys()),
            "max_concurrent": self.config.max_concurrent_sessions,
            "startup_seconds": round(elapsed, 1),
        }))

    async def _verify_hooks(self) -> None:
        """Verify AI meta-agent hooks are installed and effective."""
        result = await self.hook_verifier.verify()
        self.hook_verifier.raise_on_failure(result)

    async def _check_in_progress_issues(
        self,
        state: OrchestratorState,
        issue_branches: dict[int, str],
        issues_to_resume: list[tuple[Issue, str]],
    ) -> None:
        """Check all in-progress issues and determine action.

        Args:
            state: Orchestrator state
            issue_branches: Map of issue number to branch name
            issues_to_resume: List to append issues that need resuming
        """
        for agent_label in self.config.agents.keys():
            state.startup_message = f"Checking in-progress issues for {agent_label}..."
            api_start = time.time()

            milestones = self.config.get_filter_milestones()
            if not milestones:
                milestones = [None]
            issues = []
            for milestone in milestones:
                with gh_audit.context(
                    reason=gh_audit.AuditReason.STARTUP_REFRESH,
                    scope=gh_audit.AuditScope.STARTUP,
                ):
                    issues.extend(self.repository_host.list_issues(
                        labels=self._build_labels(agent_label, self.config.get_label_in_progress()),
                        milestone=milestone,
                        limit=self.config.issue_fetch_limit,
                    ))

            elapsed = time.time() - api_start
            logger.debug("Fetched %d in-progress issues for %s in %.1fs", len(issues), agent_label, elapsed)
            print(f"[startup] Fetched {len(issues)} in-progress issues for {agent_label} in {elapsed:.1f}s")

            for issue in issues:
                state.startup_message = f"Analyzing issue #{issue.number}..."

                # Use shared analysis logic
                analysis = analyze_issue(
                    issue=issue,
                    repo=self.config.repo,
                    issue_branches=issue_branches,
                    check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
                )

                # Skip blocked issues
                if issue.is_blocked:
                    print(f"  #{issue.number}: Blocked - waiting for intervention")
                    continue

                if analysis.has_session:
                    print(f"  #{issue.number}: Active session found - resuming monitoring")
                elif analysis.has_open_pr:
                    # S2: PR exists but issue might be missing pr-pending label
                    # Add pr-pending and remove in-progress (crash recovery)
                    if not labels.is_pr_pending(issue.labels):
                        print(f"  #{issue.number}: Has open PR - adding pr-pending label (crash recovery)")
                        self.repository_host.add_label(issue.number, labels.PR_PENDING)
                        self.repository_host.remove_label(issue.number, self.config.get_label_in_progress())
                    else:
                        print(f"  #{issue.number}: Has open PR ({analysis.pr_url or 'unknown'}) - already has pr-pending")
                elif analysis.has_partial_work:
                    print(f"  #{issue.number}: Has branch '{analysis.branch}' with commits - queuing for resume")
                    issues_to_resume.append((issue, agent_label))
                elif analysis.is_orphaned_label:
                    print(f"  #{issue.number}: No session or branch - clearing stale label")
                    self.repository_host.remove_label(issue.number, self.config.get_label_in_progress())

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

    def _recover_orphaned_cleanups(self, state: OrchestratorState) -> None:
        """Recover orphaned cleanups from before restart.

        This is a simplified version - the full logic is in CleanupManager.
        """
        # Delegate to CleanupManager if needed
        pass

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
