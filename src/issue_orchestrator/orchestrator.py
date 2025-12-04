"""Main orchestrator - ties everything together."""

import asyncio
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .github import (
    list_issues, add_label, remove_label,
    get_open_prs_for_branch, get_latest_blocked_info, get_latest_needs_human_info
)
from .models import Issue, Session, SessionStatus, OrchestratorState
from .monitor import SessionMonitor
from .scheduler import Scheduler
from .tmux import create_session, session_exists, kill_session, send_keys
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

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filter_label if configured."""
        result = list(labels)
        if self.config.filter_label:
            result.append(self.config.filter_label)
        return result

    async def startup(self) -> None:
        """Handle startup - check for stale in-progress issues."""
        print("Checking for stale in-progress issues...")

        # Get all in-progress issues for our agent types
        for agent_label in self.config.agents.keys():
            issues = list_issues(
                self.config.repo,
                labels=self._build_labels(agent_label, self.config.label_in_progress),
            )

            for issue in issues:
                # Check if we have an active session for this issue
                session_name = f"issue-{issue.number}"
                if session_exists(session_name):
                    print(f"  Issue #{issue.number} has active session - resuming monitoring")
                    # TODO: recreate Session object and add to state
                else:
                    print(f"  Issue #{issue.number} marked in-progress but no session - clearing label")
                    remove_label(self.config.repo, issue.number, self.config.label_in_progress)

    def launch_session(self, issue: Issue) -> Session:
        """Launch a new session for an issue."""
        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            raise ValueError(f"No agent config for {issue.agent_type}")

        # Create worktree (sibling to repo, named {repo}-{issue_number})
        worktree_path, branch_name = create_worktree(
            repo_root=self.config.repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
        )

        # Mark issue as in-progress
        add_label(self.config.repo, issue.number, self.config.label_in_progress)

        # Build command
        command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
        )

        # Create tmux session
        session_name = f"issue-{issue.number}"
        create_session(session_name, command, worktree_path)

        # Wait for Claude to initialize, then send the initial prompt
        time.sleep(3)
        initial_prompt = agent_config.get_initial_prompt(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
        )
        send_keys(session_name, initial_prompt)

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
        print(f"Session #{session.issue.number} completed with status: {status.value}")

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

            # If not paused and have capacity, launch more sessions
            if not self.state.paused:
                available_slots = self.config.max_sessions - len(self.state.active_sessions)

                if available_slots > 0:
                    # Get available issues
                    all_issues = []
                    for agent_label in self.config.agents.keys():
                        issues = list_issues(self.config.repo, labels=self._build_labels(agent_label))
                        all_issues.extend(issues)

                    available = self.scheduler.get_available_issues(all_issues)
                    sorted_issues = self.scheduler.sort_by_priority(available)

                    # Pick next batch
                    to_launch = self.scheduler.pick_next_batch(
                        sorted_issues,
                        len(self.state.active_sessions),
                        self.state.priority_queue,
                    )

                    for issue in to_launch:
                        try:
                            self.launch_session(issue)
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
