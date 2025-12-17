"""Main orchestrator - ties everything together."""

import asyncio
import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _emit_event(event_type: str, data: dict | None = None) -> None:
    """Emit an SSE event to connected dashboard clients.

    This is a fire-and-forget operation - if no event loop is running
    or the web module isn't imported, it silently does nothing.
    """
    try:
        from .web import broadcast_event, _event_subscribers
        # Only try to emit if there are subscribers
        if not _event_subscribers:
            logger.debug("[SSE] No subscribers, skipping event: %s", event_type)
            return
        # Schedule the async broadcast in the current event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            logger.info("[SSE] Emitting event '%s' to %d subscribers, data=%s",
                       event_type, len(_event_subscribers), data)
            asyncio.create_task(broadcast_event(event_type, data))
        else:
            logger.debug("[SSE] Event loop not running, skipping event: %s", event_type)
    except Exception as e:
        logger.warning("[SSE] Failed to emit event %s: %s", event_type, e)

from .config import Config
from .github import (
    list_issues, add_label, remove_label,
    get_open_prs_for_branch, get_latest_blocked_info, get_latest_needs_human_info,
    list_prs_with_label, create_issue,
)
from .locks import try_claim, release_claim, cleanup_stale_claims, cleanup_orphaned_claims
from .models import Issue, Session, SessionStatus, OrchestratorState, PendingReview, PendingRework
from .monitor import SessionMonitor
from .scheduler import Scheduler
from .tmux import create_session, session_exists, kill_session
from .worktree import create_worktree, remove_worktree, has_uncommitted_changes


@dataclass
class Orchestrator:
    """Main orchestrator that coordinates everything."""

    config: Config
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    monitor: SessionMonitor = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)

    def __post_init__(self):
        self.scheduler = Scheduler(self.config)
        self.monitor = SessionMonitor(self.config)
        self._iterm_manager = None  # Lazy init

    @property
    def _using_iterm2(self) -> bool:
        """Check if we're using iTerm2 mode (or web mode, which also uses iTerm2 tabs)."""
        return self.config.ui_mode in ("iterm2", "web")

    def _get_iterm_manager(self):
        """Get the iTerm2 session manager (lazy init)."""
        if self._iterm_manager is None:
            from .iterm2 import get_iterm_manager
            self._iterm_manager = get_iterm_manager()
        return self._iterm_manager

    def _extract_session_number(self, session_name: str) -> int:
        """Extract the number from a session name like 'issue-42' or 'review-123'."""
        import re
        match = re.search(r"-(\d+)$", session_name)
        if match:
            return int(match.group(1))
        raise ValueError(f"Could not extract number from session name: {session_name}")

    def _create_session(self, session_name: str, command: str, working_dir: Path, title: str | None = None) -> bool:
        """Create a session using the appropriate backend.

        Returns:
            True if session was created successfully, False otherwise.
        """
        if self._using_iterm2:
            session_number = self._extract_session_number(session_name)
            return self._get_iterm_manager().create_session(session_number, command, str(working_dir), title)
        else:
            create_session(session_name, command, working_dir, title)
            return True  # tmux create_session doesn't return status, assume success

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the appropriate backend."""
        if self._using_iterm2:
            session_number = self._extract_session_number(session_name)
            return self._get_iterm_manager().session_exists(session_number)
        else:
            return session_exists(session_name)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the appropriate backend."""
        if self._using_iterm2:
            session_number = self._extract_session_number(session_name)
            self._get_iterm_manager().kill_session(session_number)
        else:
            kill_session(session_name)

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filter_label if configured."""
        result = list(labels)
        if self.config.filter_label:
            result.append(self.config.filter_label)
        return result

    def _get_milestone_filter(self) -> str | None:
        """Get the milestone filter if configured."""
        return self.config.filter_milestone

    async def startup(self) -> None:
        """Handle startup - check for stale in-progress issues."""
        from .analysis import analyze_issue, get_issue_branches

        startup_start = time.time()
        self.state.startup_status = "running"
        self.state.startup_message = "Cleaning up stale claims..."
        logger.info("Starting up - checking for stale in-progress issues...")
        print("Checking for stale in-progress issues...")

        # Clean up stale claims (default: 60 minutes)
        cleaned = cleanup_stale_claims(prefix="issue")
        if cleaned:
            logger.info("Cleaned up %d stale issue lock claims: %s", len(cleaned), cleaned)
            print(f"  Cleaned up {len(cleaned)} stale issue lock claims: {cleaned}")

        # Also clean up stale review locks
        cleaned_reviews = cleanup_stale_claims(prefix="review")
        if cleaned_reviews:
            logger.info("Cleaned up %d stale review lock claims: %s", len(cleaned_reviews), cleaned_reviews)
            print(f"  Cleaned up {len(cleaned_reviews)} stale review lock claims: {cleaned_reviews}")

        # Clean up orphaned claims (locks without active sessions)
        # This handles cases where sessions crashed immediately (e.g., command not found)
        self.state.startup_message = "Cleaning up orphaned claims..."
        orphaned = cleanup_orphaned_claims(self._session_exists, prefix="issue")
        if orphaned:
            logger.info("Cleaned up %d orphaned issue lock claims: %s", len(orphaned), orphaned)
            print(f"  Cleaned up {len(orphaned)} orphaned issue lock claims: {orphaned}")

        # Also clean up orphaned review locks
        orphaned_reviews = cleanup_orphaned_claims(self._session_exists, prefix="review")
        if orphaned_reviews:
            logger.info("Cleaned up %d orphaned review lock claims: %s", len(orphaned_reviews), orphaned_reviews)
            print(f"  Cleaned up {len(orphaned_reviews)} orphaned review lock claims: {orphaned_reviews}")

        # Clean up idle iTerm2 tabs (tabs at shell prompt where Claude has exited)
        if self._using_iterm2:
            self.state.startup_message = "Cleaning up idle iTerm2 tabs..."
            from .iterm2 import cleanup_idle_tabs
            closed_tabs = cleanup_idle_tabs()
            if closed_tabs:
                logger.info("Closed %d idle iTerm2 tabs", closed_tabs)
                print(f"  Closed {closed_tabs} idle iTerm2 tabs")

        # Get existing branches for issue detection
        self.state.startup_message = "Scanning local branches..."
        issue_branches = get_issue_branches(self.config.repo_root)

        # Get all in-progress issues for our agent types
        self.state.startup_message = "Checking in-progress issues on GitHub..."
        for agent_label in self.config.agents.keys():
            api_start = time.time()
            issues = list_issues(
                self.config.repo,
                labels=self._build_labels(agent_label, self.config.get_label_in_progress()),
                milestone=self._get_milestone_filter(),
                limit=self.config.issue_fetch_limit,
            )
            elapsed = time.time() - api_start
            logger.debug("Fetched %d in-progress issues for %s in %.1fs", len(issues), agent_label, elapsed)
            print(f"[startup] Fetched {len(issues)} in-progress issues for {agent_label} in {elapsed:.1f}s")

            for issue in issues:
                self.state.startup_message = f"Analyzing issue #{issue.number}..."
                # Use shared analysis logic
                state = analyze_issue(
                    issue=issue,
                    repo=self.config.repo,
                    issue_branches=issue_branches,
                    check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
                )

                if state.has_session:
                    print(f"  #{issue.number}: Active session found - resuming monitoring")
                    # TODO: recreate Session object and add to state
                elif state.has_open_pr:
                    print(f"  #{issue.number}: Has open PR ({state.pr_url or 'unknown'}) - skipping")
                    # Don't clear label - PR is pending review
                elif state.has_partial_work:
                    print(f"  #{issue.number}: Has branch '{state.branch}' but no session/PR - clearing label (will resume from branch)")
                    remove_label(self.config.repo, issue.number, self.config.get_label_in_progress())
                elif state.is_orphaned_label:
                    print(f"  #{issue.number}: No session or branch - clearing stale label")
                    remove_label(self.config.repo, issue.number, self.config.get_label_in_progress())

        # Check for PRs needing code review (recovery after crash/restart)
        if self.config.code_review_agent and self.config.code_review_label:
            self.state.startup_message = "Checking PRs needing code review..."
            print("\nChecking for PRs needing code review...")
            prs = list_prs_with_label(self.config.repo, self.config.code_review_label)
            for pr in prs:
                pr_number = pr.get("number")
                if pr_number is None:
                    continue
                pr_url = pr.get("url", "")
                pr_body = pr.get("body", "")

                # Extract issue number from "Closes #N" in PR body
                import re
                issue_match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
                issue_number: int = int(issue_match.group(1)) if issue_match else pr_number

                # Verify PR was created via agent-done (has verification token)
                from .agent_done import verify_pr_token, extract_pr_verification_status
                has_marker, token = extract_pr_verification_status(pr_body)
                if has_marker:
                    is_verified = verify_pr_token(pr_body, issue_number)
                    if not is_verified:
                        logger.warning(f"PR #{pr_number}: Invalid verification token (issue #{issue_number})")
                        print(f"  PR #{pr_number}: ⚠️  Invalid verification token - manual review needed")
                else:
                    # PR created without agent-done - flag it
                    logger.warning(f"PR #{pr_number}: Missing verification token (not created via agent-done)")
                    print(f"  PR #{pr_number}: ⚠️  No verification token - created outside agent-done")

                # Check if review is already in progress
                if not self._session_exists(f"review-{pr_number}"):
                    # Queue for review
                    review = PendingReview(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        pr_url=str(pr_url),
                        branch_name=pr.get("headRefName", ""),
                    )
                    if review not in self.state.pending_reviews:
                        self.state.pending_reviews.append(review)
                        print(f"  PR #{pr_number}: Queued for code review")
                else:
                    print(f"  PR #{pr_number}: Review already in progress")

        # Run queue audit to show what will be processed
        self.state.startup_message = "Auditing queue..."
        from .audit import audit_queue, print_audit
        audit_entries = audit_queue(self.config, self.state)
        print_audit(audit_entries)

        # Cache queue issues for instant dashboard pagination
        self.state.startup_message = "Caching queue..."
        self.update_queue_cache()

        self.state.startup_status = "complete"
        self.state.startup_message = ""
        elapsed = time.time() - startup_start
        logger.info("Startup complete in %.1fs", elapsed)
        print(f"[startup] Total startup time: {elapsed:.1f}s")
        _emit_event("startup_complete")

    def launch_session(self, issue: Issue) -> Optional[Session]:
        """Launch a new session for an issue."""
        launch_start = time.time()
        logger.info("Launching session for issue #%d: %s", issue.number, issue.title)

        if issue.agent_type is None:
            raise ValueError(f"Issue #{issue.number} has no agent type label")
        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            raise ValueError(f"No agent config for {issue.agent_type}")

        # Try to claim the issue first - if another instance is working on it, skip
        if not try_claim(issue.number):
            logger.debug("Issue #%d already claimed - skipping", issue.number)
            print(f"Issue #{issue.number} already claimed by another instance - skipping")
            return None

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree (sibling to repo, named {repo}-{issue_number})
        step_start = time.time()
        logger.debug("Creating worktree for issue #%d", issue.number)
        print(f"[launch] Creating worktree for issue #{issue.number}...")
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )
        worktree_time = time.time() - step_start
        logger.debug("Worktree created in %.1fs", worktree_time)
        print(f"[launch] Worktree created in {worktree_time:.1f}s")

        # Run setup commands in worktree (e.g., npm install)
        if self.config.setup_worktree:
            step_start = time.time()
            for cmd in self.config.setup_worktree:
                logger.debug("Running setup command: %s", cmd)
                print(f"[launch] Running setup: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(worktree_path),
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    logger.warning("Setup command failed: %s\n%s", cmd, result.stderr)
                    print(f"[launch] Warning: setup command failed: {cmd}")
            setup_time = time.time() - step_start
            logger.debug("Setup commands completed in %.1fs", setup_time)
            print(f"[launch] Setup completed in {setup_time:.1f}s")

        # Mark issue as in-progress
        step_start = time.time()
        add_label(self.config.repo, issue.number, self.config.get_label_in_progress())
        label_time = time.time() - step_start
        logger.debug("Label added in %.1fs", label_time)
        print(f"[launch] Label added in {label_time:.1f}s")

        # Build command
        command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
        )

        # Create session (tmux or iTerm2 tab) - command includes the initial prompt as a CLI argument
        session_name = f"issue-{issue.number}"
        step_start = time.time()
        session_created = self._create_session(session_name, command, worktree_path, title=issue.title)
        session_time = time.time() - step_start

        if not session_created:
            # Session creation failed - clean up and return None
            logger.error("Failed to create session for issue #%d", issue.number)
            print(f"[launch] ERROR: Failed to create session for issue #{issue.number}")
            print("[launch] Is iTerm2 running with a window open?")
            # Release the claim and remove the in-progress label
            release_claim(issue.number)
            remove_label(self.config.repo, issue.number, self.config.get_label_in_progress())
            return None

        logger.debug("Session created in %.1fs", session_time)
        print(f"[launch] Session created in {session_time:.1f}s")

        # Create session object
        session = Session(
            issue=issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        self.state.active_sessions.append(session)
        total_time = time.time() - launch_start
        logger.info("Session launched for issue #%d in %.1fs (worktree=%.1fs, label=%.1fs, session=%.1fs)",
                    issue.number, total_time, worktree_time, label_time, session_time)
        print(f"Launched session for issue #{issue.number}: {issue.title}")
        _emit_event("session_started", {"issue_number": issue.number, "title": issue.title})

        return session

    def handle_session_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle a completed session."""
        from .models import SessionHistoryEntry
        from .github import get_open_prs_for_branch

        print(f"Session #{session.issue.number} completed with status: {status.value}")
        _emit_event("session_completed", {"issue_number": session.issue.number, "status": status.value})

        # Release the claim on this issue
        release_claim(session.issue.number)

        # Remove from active sessions
        self.state.active_sessions = [
            s for s in self.state.active_sessions
            if s.issue.number != session.issue.number
        ]

        # Let monitor handle label updates
        self.monitor.handle_completion(session, status)

        # Track completion
        if status == SessionStatus.COMPLETED:
            self.state.completed_today.append(session.issue.number)

        # Record in session history
        pr_url = None
        if status == SessionStatus.COMPLETED:
            prs = get_open_prs_for_branch(self.config.repo, session.branch_name)
            if prs:
                pr_url = prs[0].get("url")

        # Generate human-readable status reason
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reasons.get(status, "Unknown")

        history_entry = SessionHistoryEntry(
            issue_number=session.issue.number,
            title=session.issue.title,
            agent_type=session.issue.agent_type or "unknown",
            status=status.value,
            runtime_minutes=session.runtime_minutes,
            pr_url=pr_url,
            status_reason=status_reason,
        )
        self.state.session_history.append(history_entry)

        # Cleanup worktree only if completed successfully
        # Leave it for blocked/failed so human can investigate
        if status == SessionStatus.COMPLETED:
            try:
                remove_worktree(session.worktree_path)
            except Exception as e:
                print(f"Warning: failed to remove worktree: {e}")

            # Trigger code review immediately if configured
            # Skip if agent has skip_review set (e.g., domain-expert agents)
            if pr_url and self.config.code_review_agent and not session.agent_config.skip_review:
                logger.info(f"[REVIEW] Session #{session.issue.number} completed with PR, queuing code review")
                self.queue_code_review(
                    issue_number=session.issue.number,
                    pr_url=pr_url,
                    branch_name=session.branch_name,
                )
            elif pr_url and not self.config.code_review_agent:
                logger.info(f"[REVIEW] Session #{session.issue.number} completed but code review not configured")
            elif pr_url and session.agent_config.skip_review:
                logger.info(f"[REVIEW] Session #{session.issue.number} skipping review (skip_review=true)")
            elif not pr_url:
                logger.info(f"[REVIEW] Session #{session.issue.number} completed but no PR found")

    async def run_loop(self) -> None:
        """Main orchestration loop."""
        print("Starting orchestration loop...")
        last_cache_update = time.time()
        last_ui_update = time.time()
        ui_update_interval = 30  # Emit state_changed every 30 seconds for UI refresh

        loop_iteration = 0
        while not self._shutdown_requested:
            loop_iteration += 1
            logger.info("[LOOP] Iteration %d - active=%d, pending_reviews=%d, paused=%s",
                       loop_iteration, len(self.state.active_sessions),
                       len(self.state.pending_reviews), self.state.paused)

            try:
                # Check status of all active sessions
                for session in list(self.state.active_sessions):
                    status = self.monitor.check_session(session)

                    if status != SessionStatus.RUNNING:
                        self.handle_session_completion(session, status)

                # Process pending code reviews
                self.process_pending_reviews()

                # Scan for PRs needing rework and process them
                self.scan_needs_rework_prs()
                self.process_pending_reworks()

                # Check if CTO review should be triggered
                self.check_cto_review_trigger()

                # Check if we've hit the max issues limit for this session
                max_issues = self.config.max_issues_to_start
                hit_max_issues = max_issues > 0 and self.state.issues_started_count >= max_issues

                # If not paused, not at max issues limit, and have capacity, launch more sessions
                if not self.state.paused and not hit_max_issues:
                    available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)

                    if available_slots > 0:
                        # Get available issues
                        all_issues = []
                        for agent_label in self.config.agents.keys():
                            issues = list_issues(
                                self.config.repo,
                                labels=self._build_labels(agent_label),
                                milestone=self._get_milestone_filter(),
                                limit=self.config.issue_fetch_limit,
                            )
                            all_issues.extend(issues)

                        available = self.scheduler.get_available_issues(all_issues)

                        # Filter out issues already in session history (already processed today)
                        history_numbers = {e.issue_number for e in self.state.session_history}
                        active_numbers = {s.issue.number for s in self.state.active_sessions}
                        exclude_numbers = history_numbers | active_numbers
                        available = [i for i in available if i.number not in exclude_numbers]

                        # Filter to single issue if specified
                        if self.config.filter_issue:
                            available = [i for i in available if i.number == self.config.filter_issue]

                        sorted_issues = self.scheduler.sort_by_priority(available)

                        # Pick next batch
                        to_launch = self.scheduler.pick_next_batch(
                            sorted_issues,
                            len(self.state.active_sessions),
                            self.state.priority_queue,
                        )

                        for issue in to_launch:
                            # Check pause and limit before each launch (might change mid-batch)
                            if self.state.paused:
                                print("Paused - stopping batch launch")
                                break
                            if max_issues > 0 and self.state.issues_started_count >= max_issues:
                                break
                            try:
                                session = self.launch_session(issue)
                                if session is None:
                                    # Issue was already claimed by another instance
                                    continue
                                # Successfully launched - increment counter
                                self.state.issues_started_count += 1
                            except Exception as e:
                                print(f"Failed to launch session for #{issue.number}: {e}")

                # Periodically refresh the queue cache for the dashboard
                cache_age = time.time() - last_cache_update
                if cache_age >= self.config.queue_refresh_seconds:
                    self.update_queue_cache()
                    last_cache_update = time.time()

                # Periodically emit state_changed for UI to update runtimes
                # This ensures "Starting" transitions to "Active" as time passes
                ui_age = time.time() - last_ui_update
                if ui_age >= ui_update_interval and self.state.active_sessions:
                    _emit_event("state_changed", {
                        "active_count": len(self.state.active_sessions),
                        "sessions": [s.issue.number for s in self.state.active_sessions],
                    })
                    last_ui_update = time.time()

            except Exception as e:
                logger.exception("[LOOP] Error in iteration %d: %s", loop_iteration, e)
                print(f"[LOOP] Error in iteration {loop_iteration}: {e}")

            # Wait before next check
            await asyncio.sleep(10)

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful shutdown.

        Args:
            force: If True, kill active sessions immediately instead of waiting.
        """
        active = self.state.active_sessions
        if active:
            if force:
                print(f"Force shutdown - killing {len(active)} active session(s):")
                for s in active:
                    print(f"  #{s.issue.number}: {s.issue.title}")
                    try:
                        self._kill_session(s.tmux_session_name)
                    except Exception as e:
                        print(f"    Warning: failed to kill session: {e}")
                self.state.active_sessions = []
                print("All sessions killed. Exiting...")
            else:
                print(f"Shutdown requested - waiting for {len(active)} active session(s):")
                for s in active:
                    print(f"  #{s.issue.number}: {s.issue.title} ({s.runtime_minutes} min)")
                print("\nPress Ctrl+C again to force kill all sessions.")
        else:
            print("Shutdown requested - no active sessions, exiting...")
        self._shutdown_requested = True

    def pause(self) -> None:
        """Pause - don't start new sessions."""
        self.state.paused = True
        print("Orchestrator paused - will finish current sessions but not start new ones")
        _emit_event("paused")

    def resume(self) -> None:
        """Resume after pause."""
        self.state.paused = False
        print("Orchestrator resumed")
        _emit_event("resumed")

    def update_queue_cache(self) -> None:
        """Update the cached queue issues for instant dashboard pagination.

        This should be called after startup and periodically in run_loop.
        """
        from .audit import get_queue_issues
        try:
            queue_issues = get_queue_issues(self.config, self.state)
            self.state.cached_queue_issues = queue_issues
            logger.debug("Updated queue cache with %d issues", len(queue_issues))
        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)

    def queue_code_review(self, issue_number: int, pr_url: str, branch_name: str) -> None:
        """Queue a PR for code review.

        Called immediately after a work agent creates a PR.
        The review will be processed in the next loop iteration.

        Also ensures the needs-code-review label is added to the PR as a backup
        (in case the agent didn't use agent-done and bypassed the label).
        """
        import re
        import subprocess

        # Extract PR number from URL
        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            print(f"Warning: Could not extract PR number from {pr_url}")
            return

        pr_number = int(match.group(1))

        # Check if already queued
        for review in self.state.pending_reviews:
            if review.pr_number == pr_number:
                return

        # Fetch PR body to check verification (this is a new PR, may need verification)
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "body", "-q", ".body",
                 "--repo", self.config.repo] if self.config.repo else
                ["gh", "pr", "view", str(pr_number), "--json", "body", "-q", ".body"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                pr_body = result.stdout.strip()
                from .agent_done import extract_pr_verification_status
                has_marker, _ = extract_pr_verification_status(pr_body)
                if not has_marker:
                    logger.warning(f"PR #{pr_number}: No verification token - created outside agent-done")
                    print(f"  PR #{pr_number}: ⚠️  No verification token - created outside agent-done")
        except Exception as e:
            logger.warning(f"Could not check verification for PR #{pr_number}: {e}")

        # Add needs-code-review label as backup (idempotent - won't duplicate if already present)
        if self.config.code_review_label and self.config.repo:
            try:
                result = subprocess.run(
                    ["gh", "pr", "edit", str(pr_number), "--add-label", self.config.code_review_label,
                     "--repo", self.config.repo],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    logger.info(f"Added '{self.config.code_review_label}' label to PR #{pr_number}")
                else:
                    logger.warning(f"Could not add label to PR #{pr_number}: {result.stderr}")
            except Exception as e:
                logger.warning(f"Failed to add review label to PR #{pr_number}: {e}")

        review = PendingReview(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_url=pr_url,
            branch_name=branch_name,
        )
        self.state.pending_reviews.append(review)
        print(f"📝 Queued PR #{pr_number} for code review")

    def launch_review_session(self, review: PendingReview) -> Optional[Session]:
        """Launch a code review session for a PR.

        Similar to launch_session but for reviewing PRs instead of implementing issues.
        """
        agent_label = self.config.code_review_agent
        if not agent_label:
            return None

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            print(f"Warning: No agent config for {agent_label}")
            return None

        # Try to claim the issue (prevent duplicate reviews)
        if not try_claim(review.issue_number, prefix="review"):
            print(f"PR #{review.pr_number} already being reviewed - skipping")
            return None

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree for the review (checks out the PR branch)
        from .worktree import create_worktree
        worktree_path, _ = create_worktree(
            repo_root=repo_root,
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            branch_name=review.branch_name,  # Use existing PR branch
            enforce_hooks=False,  # Reviewer doesn't need pre-push hooks
        )

        # Build command - review agent gets PR context
        command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
        )

        # Create session
        session_name = f"review-{review.pr_number}"
        self._create_session(session_name, command, worktree_path, title=f"Review PR #{review.pr_number}")

        # Create a pseudo-issue for the session
        from .models import Issue
        pseudo_issue = Issue(
            number=review.issue_number,
            title=f"Review PR #{review.pr_number}",
            labels=[agent_label],
        )

        session = Session(
            issue=pseudo_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=review.branch_name,
        )

        self.state.active_sessions.append(session)
        print(f"🔍 Launched review session for PR #{review.pr_number}")

        # Remove from pending queue
        self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]

        return session

    def process_pending_reviews(self) -> None:
        """Process any pending code reviews.

        Called each loop iteration to launch review sessions.
        Respects max_concurrent_sessions and paused state.
        """
        if not self.config.code_review_agent:
            logger.info("[REVIEW] No code_review_agent configured - skipping")
            return

        if not self.state.pending_reviews:
            return  # Normal case when queue is empty, no logging needed

        # Don't start reviews while paused
        if self.state.paused:
            logger.info("[REVIEW] Skipping reviews - orchestrator paused")
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            logger.info("[REVIEW] Skipping reviews - no capacity (active=%d, max=%d)",
                       len(self.state.active_sessions), self.config.max_concurrent_sessions)
            return

        logger.info("[REVIEW] Processing %d pending reviews (capacity=%d)",
                   len(self.state.pending_reviews), available_slots)

        # Launch reviews up to capacity
        for review in list(self.state.pending_reviews)[:available_slots]:
            logger.info("[REVIEW] Launching review for PR #%d (issue #%d)",
                       review.pr_number, review.issue_number)
            try:
                self.launch_review_session(review)
            except Exception as e:
                logger.exception("[REVIEW] Failed to launch review for PR #%d: %s",
                                review.pr_number, e)
                print(f"[REVIEW] Failed to launch review for PR #{review.pr_number}: {e}")

    def check_cto_review_trigger(self) -> None:
        """Check if we should trigger a CTO batch review based on PR count.

        Creates a review issue if:
        - cto_review_agent is configured
        - cto_review_threshold > 0
        - Number of code-reviewed PRs >= threshold
        - No existing open CTO review issue exists
        - Orchestrator is not paused
        """
        # Don't trigger new reviews while paused
        if self.state.paused:
            return

        # Check if CTO review is configured
        if not self.config.cto_review_agent:
            return
        if self.config.cto_review_threshold <= 0:
            return

        # Label to watch: either explicit cto_review_label or code_reviewed_label
        watch_label = self.config.cto_review_label or self.config.code_reviewed_label
        if not watch_label:
            return

        # Count PRs ready for CTO review
        prs = list_prs_with_label(self.config.repo, watch_label)
        if len(prs) < self.config.cto_review_threshold:
            return

        # Check if a CTO review issue already exists (avoid duplicates)
        existing = list_issues(
            self.config.repo,
            labels=[self.config.cto_review_agent],
            limit=10,
        )
        for issue in existing:
            if "Batch Review" in issue.title or "CTO Review" in issue.title:
                return

        # Create the CTO review issue
        pr_list = "\n".join(f"- PR #{pr['number']}: {pr['title']}" for pr in prs)
        body = f"""## CTO Batch Review Triggered

{len(prs)} PRs have passed code review and are ready for CTO review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{watch_label}` to `{self.config.cto_reviewed_label}` after review.
"""
        issue_number = create_issue(
            self.config.repo,
            title=f"CTO Batch Review: {len(prs)} PRs pending",
            body=body,
            labels=[self.config.cto_review_agent],
        )
        if issue_number:
            print(f"📋 Created CTO review issue #{issue_number} for {len(prs)} PRs")

    def scan_needs_rework_prs(self) -> None:
        """Scan for PRs with needs-rework label and queue them for rework.

        Called periodically to pick up PRs where reviewers requested changes.
        """
        if not self.config.code_review_agent:
            return  # Review workflow not configured

        rework_label = self.config.get_label_needs_rework()
        prs = list_prs_with_label(self.config.repo, rework_label)

        for pr in prs:
            pr_number = pr.get("number")
            if not pr_number:
                continue

            # Check if already queued or being processed
            if any(r.pr_number == pr_number for r in self.state.pending_reworks):
                continue

            # Check if already being worked on
            if any(s.issue.number == pr_number for s in self.state.active_sessions):
                continue

            # Get the PR details to find associated issue and branch
            import re
            pr_body = pr.get("body", "")
            issue_match = re.search(r"Closes #(\d+)", pr_body)
            issue_number = int(issue_match.group(1)) if issue_match else pr_number

            branch_name = pr.get("headRefName", f"{issue_number}-rework")

            # Determine rework cycle from labels
            rework_cycle = self._get_rework_cycle_from_labels(pr.get("labels", []))

            # Check if we've exceeded max rework cycles
            if rework_cycle > self.config.max_rework_cycles:
                self._escalate_to_needs_human(pr_number, issue_number, rework_cycle)
                continue

            # Queue for rework
            rework = PendingRework(
                issue_number=issue_number,
                pr_number=pr_number,
                pr_url=pr.get("url", f"https://github.com/{self.config.repo}/pull/{pr_number}"),
                branch_name=branch_name,
                rework_cycle=rework_cycle,
            )
            self.state.pending_reworks.append(rework)
            print(f"🔄 Queued PR #{pr_number} for rework (cycle {rework_cycle})")

    def _get_rework_cycle_from_labels(self, labels: list) -> int:
        """Extract rework cycle number from PR labels.

        Looks for labels like "rework-1", "rework-2", etc.
        Returns 1 if no rework label found (first rework).
        """
        import re
        for label in labels:
            label_name = label.get("name", "") if isinstance(label, dict) else str(label)
            match = re.match(r"rework-(\d+)", label_name)
            if match:
                return int(match.group(1)) + 1  # Next cycle
        return 1  # First rework

    def _escalate_to_needs_human(self, pr_number: int, issue_number: int, rework_cycle: int) -> None:
        """Escalate PR to needs-human after max rework cycles.

        Removes needs-rework label and adds needs-human label.
        """
        needs_human_label = self.config.get_label_needs_human()
        rework_label = self.config.get_label_needs_rework()

        # Add needs-human label and remove needs-rework
        try:
            subprocess.run([
                "gh", "pr", "edit", str(pr_number),
                "--add-label", needs_human_label,
                "--remove-label", rework_label,
            ], capture_output=True, text=True, check=True)
            print(f"⚠️  PR #{pr_number} escalated to {needs_human_label} after {rework_cycle} rework cycles")

            # Post comment explaining escalation
            comment = f"""## ⚠️ Escalated to Human Review

This PR has gone through {rework_cycle - 1} rework cycles without passing review.
Maximum rework cycles ({self.config.max_rework_cycles}) exceeded.

**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""
            subprocess.run(["gh", "pr", "comment", str(pr_number), "--body", comment], capture_output=True, text=True, check=True)
        except Exception as e:
            print(f"Warning: Failed to escalate PR #{pr_number}: {e}")

    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]:
        """Launch a rework session to fix issues found in review.

        Similar to launch_session but for fixing an existing PR.
        """
        # Find the original issue to get the agent type
        issues = list_issues(self.config.repo, limit=200)
        original_issue = None
        for issue in issues:
            if issue.number == rework.issue_number:
                original_issue = issue
                break

        if not original_issue:
            print(f"Warning: Could not find issue #{rework.issue_number} for rework")
            return None

        agent_label = original_issue.agent_type
        if not agent_label:
            print(f"Warning: Issue #{rework.issue_number} has no agent label")
            return None

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            print(f"Warning: No agent config for {agent_label}")
            return None

        # Try to claim the issue (prevent duplicate rework sessions)
        if not try_claim(rework.issue_number, prefix="rework"):
            print(f"Issue #{rework.issue_number} already being reworked - skipping")
            return None

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree for rework (checks out the existing PR branch)
        worktree_path, _ = create_worktree(
            repo_root=repo_root,
            issue_number=rework.issue_number,
            issue_title=f"Rework #{rework.pr_number}",
            branch_name=rework.branch_name,  # Use existing PR branch
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )

        # Build command - include rework context
        command = agent_config.get_command(
            issue_number=rework.issue_number,
            issue_title=f"Rework PR #{rework.pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=rework.pr_number,
        )

        # Create session
        session_name = f"rework-{rework.issue_number}"
        self._create_session(session_name, command, worktree_path, title=f"Rework #{rework.issue_number}")

        session = Session(
            issue=original_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=rework.branch_name,
        )

        self.state.active_sessions.append(session)
        print(f"🔧 Launched rework session for issue #{rework.issue_number} (cycle {rework.rework_cycle})")

        # Update rework cycle label on PR
        self._update_rework_cycle_label(rework.pr_number, rework.rework_cycle)

        # Remove from pending queue and remove needs-rework label
        self.state.pending_reworks = [r for r in self.state.pending_reworks if r.pr_number != rework.pr_number]
        rework_label = self.config.get_label_needs_rework()
        try:
            subprocess.run(["gh", "pr", "edit", str(rework.pr_number), "--remove-label", rework_label],
                          capture_output=True, text=True)
        except Exception:
            pass

        return session

    def _update_rework_cycle_label(self, pr_number: int, cycle: int) -> None:
        """Update the rework cycle label on a PR."""
        try:
            # Remove old rework labels and add new one
            for i in range(1, cycle):
                try:
                    subprocess.run(["gh", "pr", "edit", str(pr_number), "--remove-label", f"rework-{i}"],
                                  capture_output=True, text=True)
                except Exception:
                    pass
            subprocess.run(["gh", "pr", "edit", str(pr_number), "--add-label", f"rework-{cycle}"],
                          capture_output=True, text=True, check=True)
        except Exception as e:
            print(f"Warning: Failed to update rework label on PR #{pr_number}: {e}")

    def process_pending_reworks(self) -> None:
        """Process any pending rework requests.

        Called each loop iteration to launch rework sessions.
        Respects max_concurrent_sessions and paused state.
        """
        if not self.state.pending_reworks:
            return

        # Don't start reworks while paused
        if self.state.paused:
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            return

        # Launch reworks up to capacity
        for rework in list(self.state.pending_reworks)[:available_slots]:
            self.launch_rework_session(rework)

    def prioritize(self, issue_number: int) -> None:
        """Add an issue to the priority queue."""
        if issue_number not in self.state.priority_queue:
            self.state.priority_queue.insert(0, issue_number)
            print(f"Issue #{issue_number} added to priority queue")


async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    """Entry point to run the orchestrator."""
    # Load config
    if config_path:
        config = Config.load(config_path)
    else:
        config = Config.find_and_load()

    orchestrator = Orchestrator(config=config)

    # Setup signal handlers with force kill on second Ctrl+C
    def handle_signal(signum, frame):
        if orchestrator._shutdown_requested:
            # Second signal - force kill
            orchestrator.request_shutdown(force=True)
        else:
            # First signal - graceful shutdown
            orchestrator.request_shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run startup checks
    await orchestrator.startup()

    # Run main loop
    await orchestrator.run_loop()

    print("Orchestrator stopped")
