"""SessionLauncher - handles launching agent sessions.

This module extracts session launching logic from the orchestrator.
It coordinates:
1. Agent configuration resolution
2. Worktree creation and setup
3. Label management during launch
4. Session creation via SessionManager
5. State machine transitions
6. Event emission

The orchestrator calls into this for all session launching, keeping
the orchestrator focused on coordination and main loop logic.
"""

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine, IssueState
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine, ReviewState

from ..config import Config, AgentConfig
from ..models import Issue, Session, PendingReview, PendingRework, get_completion_path
from ..ports import EventSink, TraceEvent, RepositoryHost
from ..ports.worktree_manager import WorktreeManager
from .session_manager import SessionManager, SessionRef, SessionContext

logger = logging.getLogger(__name__)


def log_transition(
    entity_type: str,
    number: int,
    from_state: str,
    to_state: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    """Log a state transition in a consistent, searchable format."""
    msg = f"[TRANSITION] {entity_type} #{number}: {from_state} → {to_state} ({reason})"
    logger.info(msg)
    if extra:
        logger.debug(f"[TRANSITION] #{number} extra: {extra}")


def detect_existing_work(worktree_path: Path) -> Optional[str]:
    """Check if worktree has commits ahead of main and return context for agent."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "log", "--oneline", "main..HEAD"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        commits = result.stdout.strip().split('\n')
        num_commits = len(commits)
        if num_commits == 0:
            return None

        branch_result = subprocess.run(
            ["git", "-C", str(worktree_path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        commit_list = '\n'.join(f"  - {c}" for c in commits[:10])
        if num_commits > 10:
            commit_list += f"\n  ... and {num_commits - 10} more"

        return (
            f"This worktree has {num_commits} existing commit(s) from a previous session. "
            f"Branch: {branch}. Commits: {commit_list}. "
            f"EVALUATE this existing work BEFORE starting fresh."
        )
    except Exception as e:
        logger.warning("Failed to detect existing work: %s", e)
        return None


@dataclass
class LaunchResult:
    """Result of a session launch attempt."""

    session: Optional[Session]
    success: bool
    reason: str = ""


class SessionLauncher:
    """Launches agent sessions for issues, reviews, and reworks.

    Dependencies:
    - config: Configuration with agent definitions
    - events: EventSink for trace events
    - repository_host: For label operations
    - session_manager: For terminal session operations
    - get_issue_machine: Callback to get/create issue state machines
    - get_session_machine: Callback to get/create session state machines
    - get_review_machine: Callback to get/create review state machines
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
        repository_host: RepositoryHost,
        session_manager: SessionManager,
        worktree_manager: WorktreeManager,
        session_exists_fn: Callable[[str], bool],
        create_session_fn: Callable[[str, str, Path, str | None], bool],
        get_issue_machine: Callable[[int], "IssueStateMachine"],
        get_session_machine: Callable[[str, int, int], "SessionStateMachine"],
        get_review_machine: Callable[[int, int], "ReviewStateMachine"],
        refresh_issue_fn: Optional[Callable[[int], Optional[Issue]]] = None,
        dependency_evaluator: Optional[object] = None,
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self.session_manager = session_manager
        self._worktree_manager = worktree_manager
        self._session_exists = session_exists_fn
        self._create_session = create_session_fn
        self._get_issue_machine = get_issue_machine
        self._get_session_machine = get_session_machine
        self._get_review_machine = get_review_machine
        self._refresh_issue = refresh_issue_fn
        self._dependency_evaluator = dependency_evaluator

    def launch_issue_session(
        self,
        issue: Issue,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a session for an issue.

        Args:
            issue: The issue to work on
            active_sessions: Current active sessions (for conflict detection)

        Returns:
            LaunchResult with session if successful
        """
        launch_start = time.time()
        logger.info("Launching session for issue #%d: %s", issue.number, issue.title)

        if issue.agent_type is None:
            return LaunchResult(None, False, f"Issue #{issue.number} has no agent type label")

        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {issue.agent_type}")

        # Check for conflicts
        session_name = f"issue-{issue.number}"
        if any(s.issue.number == issue.number for s in active_sessions):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "iTerm tab already running")
            return LaunchResult(None, False, "Terminal session already running")

        # CAS check: Re-verify dependencies before launching
        if self._dependency_evaluator and self._refresh_issue:
            fresh_issue = self._refresh_issue(issue.number)
            if fresh_issue and fresh_issue.body:
                report = self._dependency_evaluator.evaluate(
                    issue_number=issue.number,
                    issue_body=fresh_issue.body,
                )
                if not report.runnable:
                    log_transition(
                        "issue", issue.number, "AVAILABLE", "SKIP",
                        f"dependencies changed: {report.summary()}"
                    )
                    self.events.publish(TraceEvent(
                        name="issue.dependency_blocked",
                        data={
                            "issue_number": issue.number,
                            "issue_title": issue.title,
                            "reason": report.summary(),
                        },
                    ))
                    return LaunchResult(None, False, f"Dependencies not satisfied: {report.summary()}")

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Create worktree
        repo_root = agent_config.repo_root or self.config.repo_root
        step_start = time.time()
        print(f"[launch] Creating worktree for issue #{issue.number}...")
        worktree_info = self._worktree_manager.create(
            repo_root=repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
            worktree_base=agent_config.worktree_base,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )
        worktree_path = worktree_info.path
        branch_name = worktree_info.branch_name
        worktree_time = time.time() - step_start
        print(f"[launch] Worktree created in {worktree_time:.1f}s")

        # Run setup commands
        if self.config.setup_worktree:
            self._run_setup_commands(worktree_path)

        # Add in-progress label
        step_start = time.time()
        self.repository_host.add_label(issue.number, self.config.get_label_in_progress())
        label_time = time.time() - step_start
        print(f"[launch] Label added in {label_time:.1f}s")

        # Check for existing work
        existing_work = detect_existing_work(worktree_path)
        if existing_work:
            print("[launch] Found existing work - agent will evaluate before starting fresh")

        # Build command
        base_command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            existing_work=existing_work,
        )
        completion_path = get_completion_path(issue.agent_type)
        command = f"ORCHESTRATOR_COMPLETION_PATH='{completion_path}' {base_command}"

        # Create terminal session
        step_start = time.time()
        session_created = self._create_session(session_name, command, worktree_path, issue.title)
        session_time = time.time() - step_start

        if not session_created:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
            print(f"[launch] ERROR: Failed to create session for issue #{issue.number}")
            self.repository_host.remove_label(issue.number, self.config.get_label_in_progress())
            return LaunchResult(None, False, "Failed to create terminal session")

        log_transition("issue", issue.number, "LAUNCHING", "ACTIVE", "session launched", {"agent": issue.agent_type})

        # Create session object
        session = Session(
            issue=issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
        )

        total_time = time.time() - launch_start
        logger.info("Session launched for issue #%d in %.1fs", issue.number, total_time)
        print(f"Launched session for issue #{issue.number}: {issue.title}")

        # Emit trace event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent("session.started", {
            "issue_number": issue.number,
            "session_id": session_name,
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # State machine transitions
        self._trigger_issue_session_state_transitions(issue.number, session_name, agent_config.timeout_minutes)

        return LaunchResult(session, True)

    def launch_review_session(
        self,
        review: PendingReview,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a code review session for a PR."""
        agent_label = self.config.code_review_agent
        if not agent_label:
            return LaunchResult(None, False, "No code review agent configured")

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {agent_label}")

        # Check for conflicts
        session_name = f"review-{review.pr_number}"
        if any(s.tmux_session_name == session_name for s in active_sessions):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "iTerm tab already running")
            return LaunchResult(None, False, "Terminal session already running")

        log_transition("review", review.pr_number, "QUEUED", "LAUNCHING", "no conflicts")

        # Create worktree
        repo_root = agent_config.repo_root or self.config.repo_root
        worktree_info = self._worktree_manager.create(
            repo_root=repo_root,
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            branch_name=review.branch_name,
            enforce_hooks=False,
        )
        worktree_path = worktree_info.path

        # Build command
        base_command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
        )
        completion_path = get_completion_path(agent_label)
        command = f"ORCHESTRATOR_COMPLETION_PATH='{completion_path}' {base_command}"

        # Create session
        self._create_session(session_name, command, worktree_path, f"Review PR #{review.pr_number}")

        # Create pseudo-issue for session tracking
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
            completion_path=completion_path,
        )

        log_transition("review", review.pr_number, "LAUNCHING", "ACTIVE", "session launched")

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent("review.started", {
            "pr_number": review.pr_number,
            "issue_number": review.issue_number,
            "session_name": session_name,
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # State machine transition
        self._trigger_review_state_transition(review.pr_number, review.issue_number)

        return LaunchResult(session, True)

    def launch_rework_session(
        self,
        rework: PendingRework,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a rework session to fix issues found in review."""
        agent_config = self.config.agents.get(rework.agent_type)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {rework.agent_type}")

        issue_number = int(rework.issue_key.stable_id())

        # Try to find PR details
        prs = self.repository_host.get_prs_for_branch(f"{issue_number}-")
        if not prs:
            branch_name = f"{issue_number}-rework"
            pr_number = issue_number
        else:
            pr = prs[0]
            branch_name = pr.branch
            pr_number = pr.number

        # Check for conflicts
        session_name = f"rework-{issue_number}"
        if any(s.tmux_session_name == session_name for s in active_sessions):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "iTerm tab already running")
            return LaunchResult(None, False, "Terminal session already running")

        log_transition("rework", issue_number, "QUEUED", "LAUNCHING", f"no conflicts, cycle={rework.rework_cycle}")

        # Create worktree
        repo_root = agent_config.repo_root or self.config.repo_root
        worktree_info = self._worktree_manager.create(
            repo_root=repo_root,
            issue_number=issue_number,
            issue_title=f"Rework #{pr_number}",
            branch_name=branch_name,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )
        worktree_path = worktree_info.path

        # Build command
        base_command = agent_config.get_command(
            issue_number=issue_number,
            issue_title=f"Rework PR #{pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=pr_number,
        )
        completion_path = get_completion_path(rework.agent_type)
        command = f"ORCHESTRATOR_COMPLETION_PATH='{completion_path}' {base_command}"

        # Create session
        self._create_session(session_name, command, worktree_path, f"Rework #{issue_number}")

        # Create issue object for session tracking
        rework_issue = Issue(
            number=issue_number,
            title=f"Rework #{pr_number}",
            labels=[rework.agent_type],
        )

        session = Session(
            issue=rework_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
        )

        log_transition("rework", issue_number, "LAUNCHING", "ACTIVE", f"session launched, cycle={rework.rework_cycle}")
        print(f"🔧 Launched rework session for issue #{issue_number} (cycle {rework.rework_cycle})")

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent("rework.started", {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "session_name": session_name,
            "rework_cycle": rework.rework_cycle,
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # Update rework cycle label
        self._update_rework_cycle_label(pr_number, rework.rework_cycle)

        # Remove needs-rework label
        try:
            self.repository_host.remove_label(pr_number, self.config.get_label_needs_rework())
        except Exception:
            pass

        return LaunchResult(session, True, pr_number=pr_number)

    def _run_setup_commands(self, worktree_path: Path) -> None:
        """Run setup commands in worktree."""
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
        print(f"[launch] Setup completed in {setup_time:.1f}s")

    def _trigger_issue_session_state_transitions(
        self,
        issue_number: int,
        session_name: str,
        timeout_minutes: int,
    ) -> None:
        """Trigger state machine transitions for issue session launch."""
        from ..domain.state_machines.issue_machine import IssueState

        logger.debug(f"[STATE_MACHINE] Triggering transitions for issue #{issue_number}")
        issue_machine = self._get_issue_machine(issue_number)
        if issue_machine.state == IssueState.AVAILABLE.value:
            logger.debug(f"[STATE_MACHINE] Issue #{issue_number}: AVAILABLE -> CLAIMED")
            issue_machine.claim()
            logger.debug(f"[STATE_MACHINE] Issue #{issue_number}: CLAIMED -> IN_PROGRESS")
            issue_machine.start()

        session_machine = self._get_session_machine(session_name, issue_number, timeout_minutes)
        logger.debug(f"[STATE_MACHINE] Session {session_name}: PENDING -> STARTING")
        session_machine.launch()
        logger.debug(f"[STATE_MACHINE] Session {session_name}: STARTING -> RUNNING")
        session_machine.started()

    def _trigger_review_state_transition(self, pr_number: int, issue_number: int) -> None:
        """Trigger state machine transition for review session."""
        from ..domain.state_machines.review_machine import ReviewState

        review_machine = self._get_review_machine(pr_number, issue_number)
        if review_machine.state == ReviewState.PENDING.value:
            logger.debug(f"[STATE_MACHINE] PR #{pr_number}: PENDING -> IN_REVIEW")
            review_machine.start_review()

    def _update_rework_cycle_label(self, pr_number: int, cycle: int) -> None:
        """Update the rework cycle label on a PR."""
        try:
            for i in range(1, cycle):
                try:
                    self.repository_host.remove_label(pr_number, f"rework-{i}")
                except Exception:
                    pass
            self.repository_host.add_label(pr_number, f"rework-{cycle}")
        except Exception as e:
            print(f"Warning: Failed to update rework label on PR #{pr_number}: {e}")
