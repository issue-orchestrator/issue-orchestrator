"""Main orchestrator - ties everything together."""

import asyncio
import signal
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .github import (
    list_issues, add_label, remove_label,
    get_open_prs_for_branch, get_latest_blocked_info, get_latest_needs_human_info
)
from .locks import try_claim, release_claim, cleanup_stale_claims
from .models import Issue, Session, SessionStatus, OrchestratorState
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

    def _create_session(self, session_name: str, command: str, working_dir: Path, title: str | None = None) -> None:
        """Create a session using the appropriate backend."""
        if self._using_iterm2:
            issue_number = int(session_name.replace("issue-", ""))
            self._get_iterm_manager().create_session(issue_number, command, str(working_dir), title)
        else:
            create_session(session_name, command, working_dir, title)

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the appropriate backend."""
        if self._using_iterm2:
            issue_number = int(session_name.replace("issue-", ""))
            return self._get_iterm_manager().session_exists(issue_number)
        else:
            return session_exists(session_name)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the appropriate backend."""
        if self._using_iterm2:
            issue_number = int(session_name.replace("issue-", ""))
            self._get_iterm_manager().kill_session(issue_number)
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

        print("Checking for stale in-progress issues...")

        # Clean up stale claims (default: 60 minutes)
        cleaned = cleanup_stale_claims()
        if cleaned:
            print(f"  Cleaned up {len(cleaned)} stale lock claims: {cleaned}")

        # Get existing branches for issue detection
        issue_branches = get_issue_branches(self.config.repo_root)

        # Get all in-progress issues for our agent types
        for agent_label in self.config.agents.keys():
            issues = list_issues(
                self.config.repo,
                labels=self._build_labels(agent_label, self.config.get_label_in_progress()),
                milestone=self._get_milestone_filter(),
            )

            for issue in issues:
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

    def launch_session(self, issue: Issue) -> Optional[Session]:
        """Launch a new session for an issue."""
        if issue.agent_type is None:
            raise ValueError(f"Issue #{issue.number} has no agent type label")
        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            raise ValueError(f"No agent config for {issue.agent_type}")

        # Try to claim the issue first - if another instance is working on it, skip
        if not try_claim(issue.number):
            print(f"Issue #{issue.number} already claimed by another instance - skipping")
            return None

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree (sibling to repo, named {repo}-{issue_number})
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )

        # Mark issue as in-progress
        add_label(self.config.repo, issue.number, self.config.get_label_in_progress())

        # Build command
        command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
        )

        # Create session (tmux or iTerm2 tab) - command includes the initial prompt as a CLI argument
        session_name = f"issue-{issue.number}"
        self._create_session(session_name, command, worktree_path, title=issue.title)

        # Create session object
        session = Session(
            issue=issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        self.state.active_sessions.append(session)
        print(f"Launched session for issue #{issue.number}: {issue.title}")

        return session

    def handle_session_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle a completed session."""
        from .models import SessionHistoryEntry
        from .github import get_open_prs_for_branch

        print(f"Session #{session.issue.number} completed with status: {status.value}")

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

    async def run_loop(self) -> None:
        """Main orchestration loop."""
        print("Starting orchestration loop...")

        while not self._shutdown_requested:
            # Check status of all active sessions
            for session in list(self.state.active_sessions):
                status = self.monitor.check_session(session)

                if status != SessionStatus.RUNNING:
                    self.handle_session_completion(session, status)

            # Check if we've hit the max issues limit for this session
            max_issues = self.config.max_issues_to_start
            hit_max_issues = max_issues > 0 and self.state.issues_started_count >= max_issues

            # If not paused, not at max issues limit, and have capacity, launch more sessions
            if not self.state.paused and not hit_max_issues:
                available_slots = self.config.max_sessions - len(self.state.active_sessions)

                if available_slots > 0:
                    # Get available issues
                    all_issues = []
                    for agent_label in self.config.agents.keys():
                        issues = list_issues(
                            self.config.repo,
                            labels=self._build_labels(agent_label),
                            milestone=self._get_milestone_filter(),
                        )
                        all_issues.extend(issues)

                    available = self.scheduler.get_available_issues(all_issues)

                    # Filter out issues already in session history (already processed today)
                    history_numbers = {e.issue_number for e in self.state.session_history}
                    active_numbers = {s.issue.number for s in self.state.active_sessions}
                    exclude_numbers = history_numbers | active_numbers
                    available = [i for i in available if i.number not in exclude_numbers]

                    sorted_issues = self.scheduler.sort_by_priority(available)

                    # Pick next batch
                    to_launch = self.scheduler.pick_next_batch(
                        sorted_issues,
                        len(self.state.active_sessions),
                        self.state.priority_queue,
                    )

                    for issue in to_launch:
                        # Check limit before each launch (might hit it mid-batch)
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

            # Wait before next check
            await asyncio.sleep(10)

    def request_shutdown(self) -> None:
        """Request graceful shutdown."""
        print("Shutdown requested - waiting for active sessions...")
        self._shutdown_requested = True

    def pause(self) -> None:
        """Pause - don't start new sessions."""
        self.state.paused = True
        print("Orchestrator paused - will finish current sessions but not start new ones")

    def resume(self) -> None:
        """Resume after pause."""
        self.state.paused = False
        print("Orchestrator resumed")

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

    # Setup signal handlers
    def handle_signal(signum, frame):
        orchestrator.request_shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run startup checks
    await orchestrator.startup()

    # Run main loop
    await orchestrator.run_loop()

    print("Orchestrator stopped")
