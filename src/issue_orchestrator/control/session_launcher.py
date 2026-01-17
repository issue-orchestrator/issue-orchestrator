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

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from .dependency_evaluator import DependencyEvaluator
    from .completion_handler import CompletionHandler
    from .action_applier import ActionApplier
    from .session_manager import SessionType
    from .session_controller import SessionController
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager
    from ..observation.observer import SessionObserver
    from ..domain.models import OrchestratorState
    from ..ports.session_runner import DiscoveredSession
    from ..ports.claim_manager import ClaimManager

from ..infra.config import Config
from ..infra.logging_config import issue_log, log_context
from ..events import EventName
from ..domain.models import Issue, Session, SessionStatus, PendingReview, PendingRework, PendingTriageReview, get_completion_path, SessionKey, TaskKind
from .worktree import WorktreePreparationError
from .worktree_context import WorktreeContext
from ..ports import (
    EventSink,
    TraceEvent,
    RepositoryHost,
    Issue as IssueProtocol,
    WorkingCopy,
    CommandRunner,
)
from ..ports.session_output import SessionOutput
from ..ports.worktree_manager import WorktreeManager, WorktreeReuseOptions
from .action_applier import ActionApplier
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .session_manager import SessionManager

logger = logging.getLogger(__name__)


def log_transition(
    entity_type: str,
    number: int,
    from_state: str,
    to_state: str,
    reason: str,
    extra: dict[str, str | int | bool | None] | None = None,
) -> None:
    """Log a state transition in a consistent, searchable format."""
    msg = f"[TRANSITION] {entity_type} #{number}: {from_state} → {to_state} ({reason})"
    logger.info(msg)
    if extra:
        logger.debug(f"[TRANSITION] #{number} extra: {extra}")


def detect_existing_work(worktree_path: Path, working_copy: WorkingCopy) -> Optional[str]:
    """Check if worktree has commits ahead of main and return context for agent."""
    try:
        commits = working_copy.get_commits_ahead_of_main(worktree_path)
        if not commits:
            return None

        branch = working_copy.get_current_branch(worktree_path) or "unknown"
        commit_list = "\n".join(
            f"  - {c.short_sha} {c.message}" for c in commits[:10]
        )
        if len(commits) > 10:
            commit_list += f"\n  ... and {len(commits) - 10} more"

        return (
            f"This worktree has {len(commits)} existing commit(s) from a previous session. "
            f"Branch: {branch}. Commits: {commit_list}. "
            f"EVALUATE this existing work BEFORE starting fresh."
        )
    except Exception as e:
        logger.warning("Failed to detect existing work: %s", e)
        return None


def _build_worktree_error_comment(error: WorktreePreparationError) -> str:
    """Build a comment explaining the worktree preparation failure."""
    safe_path = error.path.name
    return (
        f"## Worktree Preparation Failed\n\n"
        f"The orchestrator could not prepare the worktree for this issue.\n\n"
        f"**Error:** {error}\n\n"
        f"**Worktree path:** `{safe_path}`\n\n"
        f"**Details:** `.issue-orchestrator/diagnostics/worktree-prep.json` in that worktree; "
        f"look under your `worktree_base` (default: parent of the repo) for `{safe_path}`.\n\n"
        f"This usually means stale files from a previous session could not be deleted. "
        f"Please manually check and clean the worktree, then remove the `blocked-needs-human` label "
        f"to allow the orchestrator to retry."
    )


def _write_worktree_diagnostic(error: WorktreePreparationError) -> None:
    """Write a local diagnostic file with full details (not posted to GitHub)."""
    diag_dir = error.path / ".issue-orchestrator" / "diagnostics"
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
        diag_path = diag_dir / "worktree-prep.json"
        diag_path.write_text(
            json.dumps(
                {
                    "issue_number": error.issue_number,
                    "worktree_path": str(error.path),
                    "error": str(error),
                },
                indent=2,
            )
            + "\n"
        )
    except Exception as exc:
        logger.warning("Failed to write worktree diagnostics: %s", exc)


@dataclass
class LaunchResult:
    """Result of a session launch attempt."""

    session: Optional[Session]
    success: bool
    reason: str = ""
    keep_queued: bool = False  # If True, don't remove from pending queue (terminal already running)


class SessionLauncher:
    """Launches agent sessions for issues, reviews, and reworks.

    Dependencies:
    - config: Configuration with agent definitions
    - events: EventSink for trace events
    - repository_host: For GitHub reads during launch
    - action_applier: For applying label/comment mutations
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
        action_applier: ActionApplier,
        session_manager: SessionManager,
        worktree_manager: WorktreeManager,
        working_copy: WorkingCopy,
        command_runner: CommandRunner,
        session_output: SessionOutput,
        session_exists_fn: Callable[[str], bool],
        create_session_fn: Callable[[str, str, Path, str | None], bool],
        get_issue_machine: Callable[["IssueProtocol"], Optional["IssueStateMachine"]],
        get_session_machine: Callable[[str, int, int], Optional["SessionStateMachine"]],
        get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
        refresh_issue_fn: Optional[Callable[[int], Optional["IssueProtocol"]]] = None,
        dependency_evaluator: Optional["DependencyEvaluator"] = None,
        claim_manager: Optional["ClaimManager"] = None,
    ):
        self.config = config
        self.events = events
        self.repository_host = repository_host
        self._action_applier = action_applier
        self.session_manager = session_manager
        self._worktree_manager = worktree_manager
        self._working_copy = working_copy
        self._command_runner = command_runner
        self._session_output = session_output
        self._session_exists = session_exists_fn
        self._create_session = create_session_fn
        self._get_issue_machine = get_issue_machine
        self._get_session_machine = get_session_machine
        self._get_review_machine = get_review_machine
        self._refresh_issue = refresh_issue_fn
        self._dependency_evaluator = dependency_evaluator
        self._claim_manager = claim_manager

    def _worktree_reuse_options(self, *, allow_remote_branch_delete: bool = True) -> WorktreeReuseOptions:
        return WorktreeReuseOptions(
            reuse_push_preflight=self.config.reuse_push_preflight,
            worktree_branch_on_recreate=self.config.worktree_branch_on_recreate,
            allow_no_verify_dry_run_preflight=self.config.allow_no_verify_dry_run_preflight,
            allow_remote_branch_delete=allow_remote_branch_delete,
        )

    def _apply_actions(self, actions: list[Action], *, context: str) -> bool:
        """Apply mutations through the ActionApplier."""
        all_ok = True
        for action in actions:
            result = self._action_applier.apply(action)
            if not result.success:
                all_ok = False
                logger.warning(
                    "[launch] Failed to apply %s (%s): %s",
                    action.action_type.value,
                    context,
                    result.error,
                )
        return all_ok

    def launch_issue_session(
        self,
        issue: "IssueProtocol",
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
        logger.info(issue_log(issue.number, "Session starting: type=code title=%s"), issue.title)

        if issue.agent_type is None:
            return LaunchResult(None, False, f"Issue #{issue.number} has no agent type label")

        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {issue.agent_type}")

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")
        issue_key = issue.key
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

        # Check for conflicts
        session_name = f"issue-{issue.number}"
        if any(s.issue.number == issue.number for s in active_sessions):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running")

        logger.info(
            "[launch] Issue session identity: issue=%s issue_key=%s agent=%s task=%s session=%s",
            issue.number,
            issue_key,
            issue.agent_type,
            TaskKind.CODE.value,
            session_name,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Issue session key: issue=%s session=%s session_key=%s",
            issue.number,
            session_name,
            session_key.stable_id(),
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )

        # CAS check: Re-verify dependencies before launching
        if self._dependency_evaluator and self._refresh_issue:
            fresh_issue = self._refresh_issue(issue.number)
            if fresh_issue and fresh_issue.body:
                report = self._dependency_evaluator.evaluate(
                    issue_number=issue.number,
                    issue_body=fresh_issue.body,
                    source_milestone=fresh_issue.milestone,
                )
                if not report.runnable:
                    log_transition(
                        "issue", issue.number, "AVAILABLE", "SKIP",
                        f"dependencies changed: {report.summary()}"
                    )
                    self.events.publish(TraceEvent(
                        EventName.ISSUE_DEPENDENCY_BLOCKED,
                        {
                            "issue_number": issue.number,
                            "issue_title": issue.title,
                            "reason": report.summary(),
                        },
                    ))
                    return LaunchResult(None, False, f"Dependencies not satisfied: {report.summary()}")

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Acquire claim if claim manager is configured
        lease_id: str | None = None
        lease_acquired_at: datetime | None = None
        lease_expires_at: datetime | None = None

        if self._claim_manager:
            logger.info(issue_log(issue.number, "Acquiring claim..."))
            claim_result = self._claim_manager.attempt_claim(issue.number)

            if not claim_result.success:
                log_transition("issue", issue.number, "LAUNCHING", "CLAIM_FAILED", f"claim attempt failed: {claim_result.error}")
                self.events.publish(TraceEvent(
                    EventName.CLAIM_CONTESTED,
                    {
                        "issue_number": issue.number,
                        "issue_title": issue.title,
                        "error": claim_result.error,
                    },
                ))
                return LaunchResult(None, False, f"Failed to claim issue: {claim_result.error}")

            # Run convergence to confirm ownership
            logger.info(issue_log(issue.number, "Running claim convergence..."))
            converged = self._claim_manager.run_convergence(issue.number, claim_result.lease_id or "")

            if not converged:
                log_transition("issue", issue.number, "LAUNCHING", "CLAIM_LOST", "convergence failed - another claimant won")
                self._claim_manager.release_claim(issue.number, claim_result.lease_id or "")
                self.events.publish(TraceEvent(
                    EventName.CLAIM_LOST,
                    {
                        "issue_number": issue.number,
                        "issue_title": issue.title,
                        "lease_id": claim_result.lease_id,
                        "reason": "convergence_failed",
                    },
                ))
                return LaunchResult(None, False, "Claim convergence failed - another orchestrator won")

            # Claim acquired successfully
            lease_id = claim_result.lease_id
            lease_acquired_at = datetime.now()
            lease_expires_at = lease_acquired_at + timedelta(seconds=self.config.claim_lease_seconds if hasattr(self.config, 'claim_lease_seconds') else 900)
            logger.info(issue_log(issue.number, "Claim acquired: lease_id=%s"), lease_id)
            self.events.publish(TraceEvent(
                EventName.CLAIM_ACQUIRED,
                {
                    "issue_number": issue.number,
                    "lease_id": lease_id,
                },
            ))

        # Create and prepare worktree using WorktreeContext
        step_start = time.time()
        logger.info(issue_log(issue.number, "Creating worktree..."))

        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=issue.number,
            issue_title=issue.title,
            session_name=session_name,
            agent_label=issue.agent_type,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(),
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self.config.get_label_needs_human()
            self._apply_actions([
                AddLabelAction(
                    issue_number=issue.number,
                    label=needs_human_label,
                    reason="worktree preparation failed",
                ),
                AddCommentAction(
                    number=issue.number,
                    comment=_build_worktree_error_comment(ctx.error),
                    reason="worktree preparation failed",
                ),
            ], context="worktree_prepare_issue")
            self.events.publish(TraceEvent(
                EventName.ISSUE_NEEDS_HUMAN,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "reason": str(ctx.error),
                },
            ))
            # Release claim if we acquired one
            if self._claim_manager and lease_id:
                self._claim_manager.release_claim(issue.number, lease_id)
                logger.info(issue_log(issue.number, "Released claim after worktree failure: lease_id=%s"), lease_id)
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        # Extract values from context for local use
        worktree_path = ctx.worktree_path
        branch_name = ctx.branch_name
        worktree_info = ctx.worktree_info
        run = ctx.run
        claude_project_dir = ctx.claude_project_dir

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.CODE.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": issue.agent_type,
        })

        logger.info(
            "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
            run.run_id,
            session_name,
            issue.number,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Issue session paths: issue=%s worktree=%s branch=%s",
            issue.number,
            worktree_path,
            branch_name,
        )
        logger.info(
            "[launch] Claude project dir: session=%s path=%s exists=%s",
            session_name,
            claude_project_dir,
            claude_project_dir.exists(),
        )

        worktree_time = time.time() - step_start
        logger.info(
            issue_log(issue.number, "Worktree ready: path=%s branch=%s rebase_status=%s time=%.1fs"),
            worktree_path, branch_name, "CONFLICT" if worktree_info.rebase_failed else "ok", worktree_time
        )

        # Run setup commands
        if self.config.setup_worktree:
            self._run_setup_commands(worktree_path)

        # Add in-progress label
        step_start = time.time()
        in_progress_label = self.config.get_label_in_progress()
        label_ok = self._apply_actions([
            AddLabelAction(
                issue_number=issue.number,
                label=in_progress_label,
                reason="session launched",
            ),
        ], context="launch_in_progress_label")
        if not label_ok:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "in-progress label failed")
            logger.error(issue_log(issue.number, "FAILED: could not add in-progress label"))
            self.events.publish(TraceEvent(
                EventName.SESSION_START_FAILED,
                {
                    "issue_number": issue.number,
                    "session_name": session_name,
                    "reason": "in_progress_label_failed",
                },
            ))
            try:
                self._worktree_manager.remove(worktree_path)
                logger.info(issue_log(issue.number, "Cleaned up worktree after launch failure: %s"), worktree_path)
            except Exception as e:
                logger.warning(issue_log(issue.number, "Failed to remove worktree after launch failure: %s"), e)
            # Release claim if we acquired one
            if self._claim_manager and lease_id:
                self._claim_manager.release_claim(issue.number, lease_id)
                logger.info(issue_log(issue.number, "Released claim after label failure: lease_id=%s"), lease_id)
            return LaunchResult(None, False, "Failed to add in-progress label")
        label_time = time.time() - step_start
        logger.info("[launch] Label added in %.1fs", label_time)

        # Check for existing work and rebase status
        existing_work = detect_existing_work(worktree_path, self._working_copy)
        if existing_work:
            logger.info("[launch] Found existing work - agent will evaluate before starting fresh")

        # Add merge conflict warning if rebase failed
        if worktree_info.rebase_failed:
            conflict_warning = (
                "WARNING: This branch could not be rebased onto main due to merge conflicts. "
                "The code is out of date. You should resolve the conflicts by running: "
                "git fetch origin main && git rebase origin/main. "
                "If conflicts occur, resolve them and continue with: git rebase --continue. "
                "This is critical to ensure tests pass with the latest code."
            )
            if existing_work:
                existing_work = f"{existing_work}\n\n{conflict_warning}"
            else:
                existing_work = conflict_warning
            logger.warning("[launch] Rebase failed - agent will need to resolve merge conflicts")

        # Build command
        base_command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            existing_work=existing_work,
        )
        completion_path = get_completion_path(issue.agent_type, session_name=session_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        # Export env vars so child processes (like agent-done) can access them
        env_exports = f"export ORCHESTRATOR_COMPLETION_PATH='{completion_path}'"
        env_exports += f" ORCHESTRATOR_AGENT_LABEL='{issue.agent_type}'"
        env_exports += f" ORCHESTRATOR_ISSUE_NUMBER='{issue.number}'"
        env_exports += f" ORCHESTRATOR_API_PORT='{self.config.web_port}'"
        # NOTE: Validation config is NOT passed via env var.
        # agent-done reads validation config from the worktree's config file.
        # This ensures tests are deterministic (no env var leakage).

        if self.config.e2e_pr_labels:
            labels_str = ",".join(self.config.e2e_pr_labels)
            env_exports += f" E2E_PR_LABELS='{labels_str}'"
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Issue session command: issue=%s session=%s worktree=%s completion=%s command=%s",
            issue.number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        # Create terminal session
        step_start = time.time()
        session_created = self._create_session(session_name, command, worktree_path, issue.title)
        logger.info(
            "[launch] Issue session create result: issue=%s session=%s created=%s",
            issue.number,
            session_name,
            session_created,
        )
        _session_time = time.time() - step_start

        if not session_created:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
            logger.error(issue_log(issue.number, "FAILED: session creation failed"))
            self._apply_actions([
                RemoveLabelAction(
                    issue_number=issue.number,
                    label=self.config.get_label_in_progress(),
                    reason="session creation failed",
                ),
            ], context="launch_session_creation_failed")
            # Release claim if we acquired one
            if self._claim_manager and lease_id:
                self._claim_manager.release_claim(issue.number, lease_id)
                logger.info(issue_log(issue.number, "Released claim after session creation failure: lease_id=%s"), lease_id)
            return LaunchResult(None, False, "Failed to create terminal session")

        log_transition("issue", issue.number, "LAUNCHING", "ACTIVE", "session launched", {"agent": issue.agent_type})

        # Create session object with domain identity
        session = Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
            agent_label=issue.agent_type,
            lease_id=lease_id,
            lease_acquired_at=lease_acquired_at,
            lease_expires_at=lease_expires_at,
        )

        total_time = time.time() - launch_start
        logger.info(
            issue_log(issue.number, "Session launched: type=code agent=%s time=%.1fs"),
            issue.agent_type, total_time
        )

        # Emit trace event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent(EventName.SESSION_STARTED, {
            "issue_number": issue.number,
            "session_id": session_name,
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # State machine transitions
        self._trigger_issue_session_state_transitions(issue, session_name, agent_config.timeout_minutes)

        return LaunchResult(session, True)

    def launch_review_session(
        self,
        review: PendingReview,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a code review session for a PR."""
        # Get the reviewer for this agent (per-agent override or default)
        agent_label = self.config.get_reviewer_for_agent(review.agent_label) if review.agent_label else self.config.code_review_agent
        if not agent_label:
            return LaunchResult(None, False, "No code review agent configured")

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {agent_label}")

        # Check for conflicts
        session_name = f"review-{review.pr_number}"
        if any(s.terminal_id == session_name for s in active_sessions):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")
        issue_key = review.issue_key
        session_key = SessionKey(issue=issue_key, task=TaskKind.REVIEW)
        log_transition("review", review.pr_number, "QUEUED", "LAUNCHING", "no conflicts")
        logger.info(
            "[launch] Review session identity: issue=%s issue_key=%s pr=%s agent=%s task=%s session=%s branch=%s",
            review.issue_number,
            issue_key,
            review.pr_number,
            agent_label,
            TaskKind.REVIEW.value,
            session_name,
            review.branch_name,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Review session key: issue=%s pr=%s session=%s session_key=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            session_key.stable_id(),
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )

        # Create and prepare worktree using WorktreeContext
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            session_name=session_name,
            agent_label=agent_label,
            branch_name=review.branch_name,
            enforce_hooks=False,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("review", review.pr_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(review.issue_number, "BLOCKED: worktree preparation failed for review: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self.config.get_label_needs_human()
            self._apply_actions([
                AddLabelAction(
                    issue_number=review.issue_number,
                    label=needs_human_label,
                    reason="worktree preparation failed",
                ),
                AddCommentAction(
                    number=review.issue_number,
                    comment=_build_worktree_error_comment(ctx.error),
                    reason="worktree preparation failed",
                ),
            ], context="worktree_prepare_review")
            self.events.publish(TraceEvent(
                EventName.ISSUE_NEEDS_HUMAN,
                {
                    "issue_number": review.issue_number,
                    "pr_number": review.pr_number,
                    "reason": str(ctx.error),
                },
            ))
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        # Extract values from context
        worktree_path = ctx.worktree_path
        worktree_info = ctx.worktree_info
        run = ctx.run
        claude_project_dir = ctx.claude_project_dir

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.REVIEW.value,
            "issue_key": issue_key.stable_id(),
            "pr_number": review.pr_number,
            "session_key": session_key.stable_id(),
            "agent": agent_label,
        })

        logger.info(
            "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
            run.run_id,
            session_name,
            review.issue_number,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Review session paths: issue=%s pr=%s worktree=%s branch=%s",
            review.issue_number,
            review.pr_number,
            worktree_path,
            review.branch_name,
        )
        logger.info(
            "[launch] Claude project dir: session=%s path=%s exists=%s",
            session_name,
            claude_project_dir,
            claude_project_dir.exists(),
        )

        # Check if rebase failed (PR branch couldn't be updated to latest main)
        existing_work: str | None = None
        if worktree_info.rebase_failed:
            existing_work = (
                "WARNING: This PR branch could not be rebased onto main due to merge conflicts. "
                "The branch is behind main. When reviewing, consider whether merge conflicts "
                "need to be resolved before the PR can be merged."
            )
            logger.warning("[launch] Rebase failed for review - PR branch is behind main")

        # Build command
        base_command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
        )
        completion_path = get_completion_path(agent_label, session_name=session_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        # Export env vars so child processes (like agent-done) can access them
        env_exports = f"export ORCHESTRATOR_COMPLETION_PATH='{completion_path}'"
        env_exports += f" ORCHESTRATOR_AGENT_LABEL='{agent_label}'"
        env_exports += f" ORCHESTRATOR_ISSUE_NUMBER='{review.issue_number}'"
        env_exports += f" ORCHESTRATOR_API_PORT='{self.config.web_port}'"
        # NOTE: Validation config is NOT passed via env var.
        # agent-done reads validation config from the worktree's config file.
        # This ensures tests are deterministic (no env var leakage).

        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Review session command: issue=%s pr=%s session=%s worktree=%s completion=%s command=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        # Create session
        session_created = self._create_session(session_name, command, worktree_path, f"Review PR #{review.pr_number}")
        logger.info(
            "[launch] Review session create result: issue=%s pr=%s session=%s created=%s",
            review.issue_number,
            review.pr_number,
            session_name,
            session_created,
        )

        # Create pseudo-issue for session tracking
        pseudo_issue = Issue(
            number=review.issue_number,
            title=f"Review PR #{review.pr_number}",
            labels=[agent_label],
        )

        # Create session with domain identity (REVIEW task type)
        session = Session(
            key=session_key,
            issue=pseudo_issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=review.branch_name,
            completion_path=completion_path,
            agent_label=agent_label,
        )

        log_transition("review", review.pr_number, "LAUNCHING", "ACTIVE", "session launched")

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent(EventName.REVIEW_STARTED, {
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

        issue_key = rework.issue_key
        session_key = SessionKey(issue=issue_key, task=TaskKind.REWORK)
        issue_number = rework.resolve_issue_number()
        if issue_number is None:
            return LaunchResult(None, False, f"Unresolved issue number for rework {issue_key}")

        # Try to find PR details
        prs = self.repository_host.get_prs_for_issue(issue_number)
        if not prs:
            branch_name = f"{issue_number}-rework"
            pr_number = issue_number
        else:
            pr = prs[0]
            branch_name = pr.branch
            pr_number = pr.number

        # Check for conflicts
        session_name = f"rework-{issue_number}"
        if any(s.terminal_id == session_name for s in active_sessions):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

        log_transition("rework", issue_number, "QUEUED", "LAUNCHING", f"no conflicts, cycle={rework.rework_cycle}")
        logger.info(
            "[launch] Rework session identity: issue=%s issue_key=%s pr=%s agent=%s task=%s session=%s branch=%s cycle=%s",
            issue_number,
            issue_key,
            pr_number,
            rework.agent_type,
            TaskKind.REWORK.value,
            session_name,
            branch_name,
            rework.rework_cycle,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Rework session key: issue=%s pr=%s session=%s session_key=%s",
            issue_number,
            pr_number,
            session_name,
            session_key.stable_id(),
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )

        # Create and prepare worktree using WorktreeContext
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=issue_number,
            issue_title=f"Rework #{pr_number}",
            session_name=session_name,
            agent_label=rework.agent_type,
            branch_name=branch_name,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("rework", issue_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue_number, "BLOCKED: worktree preparation failed for rework: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self.config.get_label_needs_human()
            self._apply_actions([
                AddLabelAction(
                    issue_number=issue_number,
                    label=needs_human_label,
                    reason="worktree preparation failed",
                ),
                AddCommentAction(
                    number=issue_number,
                    comment=_build_worktree_error_comment(ctx.error),
                    reason="worktree preparation failed",
                ),
            ], context="worktree_prepare_rework")
            self.events.publish(TraceEvent(
                EventName.ISSUE_NEEDS_HUMAN,
                {
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "reason": str(ctx.error),
                },
            ))
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        # Extract values from context
        worktree_path = ctx.worktree_path
        worktree_info = ctx.worktree_info
        run = ctx.run
        claude_project_dir = ctx.claude_project_dir

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.REWORK.value,
            "issue_key": issue_key.stable_id(),
            "pr_number": pr_number,
            "session_key": session_key.stable_id(),
            "agent": rework.agent_type,
            "rework_cycle": rework.rework_cycle,
        })

        logger.info(
            "[SESSION_RUN_START] run_id=%s session=%s issue=%s",
            run.run_id,
            session_name,
            issue_number,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        logger.info(
            "[launch] Rework session paths: issue=%s pr=%s worktree=%s branch=%s",
            issue_number,
            pr_number,
            worktree_path,
            branch_name,
        )
        logger.info(
            "[launch] Claude project dir: session=%s path=%s exists=%s",
            session_name,
            claude_project_dir,
            claude_project_dir.exists(),
        )

        # Check if rebase failed (rework branch couldn't be updated to latest main)
        existing_work: str | None = None
        if worktree_info.rebase_failed:
            existing_work = (
                "WARNING: This branch could not be rebased onto main due to merge conflicts. "
                "The code is out of date. You should resolve the conflicts by running: "
                "git fetch origin main && git rebase origin/main. "
                "If conflicts occur, resolve them and continue with: git rebase --continue. "
                "This is critical to ensure tests pass with the latest code."
            )
            logger.warning("[launch] Rebase failed for rework - agent will need to resolve merge conflicts")

        # Build command
        base_command = agent_config.get_command(
            issue_number=issue_number,
            issue_title=f"Rework PR #{pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=pr_number,
            existing_work=existing_work,
        )
        completion_path = get_completion_path(rework.agent_type, session_name=session_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        # Export env vars so child processes (like agent-done) can access them
        env_exports = f"export ORCHESTRATOR_COMPLETION_PATH='{completion_path}'"
        env_exports += f" ORCHESTRATOR_AGENT_LABEL='{rework.agent_type}'"
        env_exports += f" ORCHESTRATOR_ISSUE_NUMBER='{issue_number}'"
        env_exports += f" ORCHESTRATOR_API_PORT='{self.config.web_port}'"
        # NOTE: Validation config is NOT passed via env var.
        # agent-done reads validation config from the worktree's config file.
        # This ensures tests are deterministic (no env var leakage).

        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Rework session command: issue=%s pr=%s session=%s worktree=%s completion=%s command=%s",
            issue_number,
            pr_number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        # Create session
        session_created = self._create_session(session_name, command, worktree_path, f"Rework #{issue_number}")
        logger.info(
            "[launch] Rework session create result: issue=%s pr=%s session=%s created=%s",
            issue_number,
            pr_number,
            session_name,
            session_created,
        )

        # Create issue object for session tracking
        rework_issue = Issue(
            number=issue_number,
            title=f"Rework #{pr_number}",
            labels=[rework.agent_type],
        )

        # Create session with domain identity (REWORK task type)
        session = Session(
            key=session_key,
            issue=rework_issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
            agent_label=rework.agent_type,
        )

        log_transition("rework", issue_number, "LAUNCHING", "ACTIVE", f"session launched, cycle={rework.rework_cycle}")
        logger.info("Launched rework session for issue #%d (cycle %d)", issue_number, rework.rework_cycle)

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent(EventName.REWORK_STARTED, {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "session_name": session_name,
            "rework_cycle": rework.rework_cycle,
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # Update rework cycle label
        self._update_rework_cycle_label(pr_number, issue_number, rework.rework_cycle)

        # Remove needs-rework label
        self._apply_actions([
            RemoveLabelAction(
                issue_number=pr_number,
                label=self.config.get_label_needs_rework(),
                reason="rework started",
            ),
        ], context="rework_remove_needs_rework")
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "issue_key": str(issue_number),
            "removed": [self.config.get_label_needs_rework()],
        }))

        return LaunchResult(session, True)

    def _run_setup_commands(self, worktree_path: Path) -> None:
        """Run setup commands in worktree."""
        step_start = time.time()
        for cmd in self.config.setup_worktree:
            logger.debug("Running setup command: %s", cmd)
            logger.info("[launch] Running setup: %s", cmd)
            result = self._command_runner.run(
                cmd,
                shell=True,
                cwd=worktree_path,
            )
            if result.returncode != 0:
                logger.warning("Setup command failed: %s\n%s", cmd, result.stderr)
                logger.warning("[launch] Setup command failed: %s", cmd)
            if result.timed_out:
                logger.warning("[launch] Setup command timed out: %s", cmd)
        setup_time = time.time() - step_start
        logger.info("[launch] Setup completed in %.1fs", setup_time)

    def _trigger_issue_session_state_transitions(
        self,
        issue: "IssueProtocol",
        session_name: str,
        timeout_minutes: int,
    ) -> None:
        """Trigger state machine transitions for issue session launch."""
        from ..domain.state_machines.issue_machine import IssueState

        logger.debug(f"[STATE_MACHINE] Triggering transitions for issue #{issue.number}")
        issue_machine = self._get_issue_machine(issue)
        if issue_machine.state == IssueState.AVAILABLE.value:
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: AVAILABLE -> CLAIMED")
            issue_machine.claim()
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: CLAIMED -> IN_PROGRESS")
            issue_machine.start()

        session_machine = self._get_session_machine(session_name, issue.number, timeout_minutes)
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

    def _update_rework_cycle_label(self, pr_number: int, issue_number: int, cycle: int) -> None:
        """Update the rework cycle label on a PR."""
        actions: list[Action] = []
        removed: list[str] = []
        for i in range(1, cycle):
            label = f"rework-cycle-{i}"
            removed.append(label)
            actions.append(RemoveLabelAction(
                issue_number=pr_number,
                label=label,
                reason="rework cycle update",
            ))
        added_label = f"rework-cycle-{cycle}"
        actions.append(AddLabelAction(
            issue_number=pr_number,
            label=added_label,
            reason="rework cycle update",
        ))
        self._apply_actions(actions, context="rework_cycle_label")
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "issue_key": str(issue_number),
            "added": [added_label],
            "removed": removed,
        }))


def handle_session_completion(
    session: Session,
    status: SessionStatus,
    state: "OrchestratorState",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    observer: "SessionObserver",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    pr_url_hint: Optional[str] = None,
    processing_errors: Optional[list[str]] = None,
    diagnostic_path: Optional[str] = None,
    validation_error: Optional[str] = None,
    validation_error_file: Optional[str] = None,
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
) -> None:
    """Handle session completion - moved from Orchestrator per method table.

    Args:
        session: The completed session
        status: The session status
        state: Orchestrator state (active_sessions, session_history, etc.)
        completion_handler: For processing completion
        action_applier: For applying actions
        observer: For cleanup
        worktree_manager: For worktree removal
        kill_session_fn: Function to kill terminal session
        config: Configuration
        pr_url_hint: Optional PR URL from completion processor (for dry-run mode)
        processing_errors: Errors from completion processor (push failed, PR creation failed, etc.)
        diagnostic_path: Path to detailed failure diagnostic file (in worktree)
        validation_error: Validation error message (for retry prompt)
        validation_error_file: Path to validation error file (for retry prompt)
        claim_manager: Optional ClaimManager for releasing claims on completion
        events: Optional EventSink for emitting claim events
    """
    from ..domain.models import DiscoveredReview, DiscoveredFailure, PendingValidationRetry

    name = session.terminal_id
    entity = "review" if name.startswith("review-") else ("rework" if name.startswith("rework-") else "issue")
    log_transition(entity, session.issue.number, "ACTIVE", status.value.upper(), f"runtime={session.runtime_minutes}min")

    # Remove by session name, NOT issue number - multiple sessions can share an issue number
    state.active_sessions = [s for s in state.active_sessions if s.terminal_id != session.terminal_id]

    # Handle validation retry - queue for re-launch instead of normal completion
    if status == SessionStatus.NEEDS_VALIDATION_RETRY:
        logger.info(
            "[COMPLETION] Issue #%d needs validation retry (attempt %d), queueing for re-launch",
            session.issue.number,
            session.validation_retry_count + 1,
        )
        state.pending_validation_retries.append(PendingValidationRetry(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            agent_label=session.agent_label or "",
            worktree_path=str(session.worktree_path),
            branch_name=session.branch_name,
            original_prompt=session.original_prompt,
            validation_error=validation_error or "",
            validation_error_file=validation_error_file,
            retry_count=session.validation_retry_count,
            validation_cmd=config.validation_cmd if hasattr(config, 'validation_cmd') else None,
        ))
        # Kill the terminal session but don't cleanup worktree (agent will continue there)
        kill_session_fn(session.terminal_id)
        return  # Skip normal completion processing

    # Process completion through CompletionHandler (includes policy decisions)
    if status == SessionStatus.COMPLETED:
        state.completed_today.append(session.issue.number)
    result = completion_handler.process_completion(
        session, status, pr_url_hint=pr_url_hint,
        processing_errors=processing_errors, diagnostic_path=diagnostic_path
    )
    if session.worktree_path:
        from ..execution.session_output_adapter import FileSystemSessionOutput
        session_output = FileSystemSessionOutput()
        run_dir = session_output.find_run_dir(session.worktree_path, session.terminal_id)
        if run_dir:
            session_output.attach_claude_log(run_dir)

    # Apply completion actions (from CompletionHandler policy)
    if result.actions:
        logger.info(
            "[COMPLETION] Applying %d actions for issue #%d status=%s: %s",
            len(result.actions),
            session.issue.number,
            status.value,
            [type(a).__name__ for a in result.actions],
        )
        action_applier.apply_all(list(result.actions))
    else:
        logger.warning(
            "[COMPLETION] No actions generated for issue #%d status=%s",
            session.issue.number,
            status.value,
        )

    # Observer handles session-level cleanup (kill sessions, close tabs)
    observer.handle_completion(session, status)

    # Release claim if session had one
    if claim_manager and session.lease_id:
        try:
            claim_manager.release_claim(session.issue.number, session.lease_id)
            logger.info(
                "[COMPLETION] Released claim for issue #%d: lease_id=%s",
                session.issue.number,
                session.lease_id,
            )
            if events:
                events.publish(TraceEvent(
                    EventName.CLAIM_RELEASED,
                    {
                        "issue_number": session.issue.number,
                        "lease_id": session.lease_id,
                        "status": status.value,
                    },
                ))
        except Exception as e:
            logger.warning(
                "[COMPLETION] Failed to release claim for issue #%d: %s",
                session.issue.number,
                e,
            )

    state.session_history.append(result.history_entry)
    if result.should_defer_cleanup and result.pending_cleanup:
        state.pending_cleanups.append(result.pending_cleanup)
    else:
        # Record immediate cleanup as a fact for the Planner to handle
        from ..domain.models import ImmediateCleanup
        state.immediate_cleanups.append(ImmediateCleanup(
            issue_number=session.issue.number,
            terminal_id=session.terminal_id,
            worktree_path=str(session.worktree_path),
            reason=status.value,
        ))

    if result.should_queue_review and result.pr_url and result.pr_number:
        state.discovered_reviews.append(DiscoveredReview(
            session.issue.number, result.pr_number, result.pr_url, session.branch_name,
            agent_label=session.agent_label
        ))
    if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
        state.discovered_failures.append(DiscoveredFailure(session.issue.number, session.issue.title, status.value))
        # Track failed issues to prevent immediate retry (cleared on cache refresh)
        state.failed_this_cycle.add(session.issue.number)
        logger.info(
            "[COMPLETION] Issue #%d added to failed_this_cycle (prevents retry until cache refresh)",
            session.issue.number,
        )

        # Surface AI session logs for debugging
        _surface_failure_context(session, status)


def _surface_failure_context(session: Session, status: SessionStatus) -> None:
    """Surface AI session logs when a session fails.

    Extracts and logs relevant failure context from the AI system's logs
    to help users understand why a session failed.
    """
    try:
        from ..adapters.session_log.registry import get_log_provider
        from ..ports.session_log import detect_ai_system_from_command

        # Detect AI system from the command used to launch the session
        ai_system = detect_ai_system_from_command(session.agent_config.command) or "claude-code"
        provider = get_log_provider(ai_system)

        # Build diagnostic header
        diag_lines = [
            f"## Session Failure Diagnostic for Issue #{session.issue.number}",
            f"- Status: {status.value}",
            f"- Agent: {session.agent_label or 'unknown'}",
            f"- AI System: {ai_system}",
            f"- Permission Mode: {session.agent_config.permission_mode}",
            f"- Worktree: {session.worktree_path}",
            f"- Runtime: {session.runtime_minutes} minutes",
        ]

        # Check for common misconfigurations
        if session.agent_config.permission_mode == "default":
            diag_lines.append("")
            diag_lines.append("⚠️  WARNING: permission_mode is 'default' - Claude will prompt for permissions!")
            diag_lines.append("   This causes sessions to hang/fail in non-interactive mode.")
            diag_lines.append("   FIX: Add 'permission_mode: bypassPermissions' to your agent config in YAML.")

        # Get log path and context
        log_path = None
        context = None
        if provider:
            log_path = provider.get_log_path(session.worktree_path, session.terminal_id)
            if log_path:
                diag_lines.append(f"- Log file: {log_path}")
                context = provider.get_failure_context(log_path)
            else:
                diag_lines.append(f"- Log file: NOT FOUND (check ~/.claude/projects/)")

        if context:
            diag_lines.append("")
            diag_lines.append(context)
        else:
            diag_lines.append("")
            diag_lines.append("No detailed failure context available from AI logs.")

        # Add troubleshooting hints
        diag_lines.append("")
        diag_lines.append("## Next Steps")
        diag_lines.append("1. Check the log file above for errors")
        diag_lines.append("2. Run: grep '[FAILURE_CONTEXT]' ~/.issue-orchestrator.log")
        diag_lines.append("3. See troubleshooting docs: /troubleshooting skill")

        logger.warning(
            "[FAILURE_CONTEXT] Issue #%d (%s):\n%s",
            session.issue.number,
            status.value,
            "\n".join(diag_lines),
        )

    except Exception as e:
        logger.warning("[FAILURE_CONTEXT] Could not extract failure context for #%d: %s", session.issue.number, e)


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Orchestrator wrapper for launching review sessions - moved per method table.

    Args:
        review: The pending review to launch
        state: Orchestrator state (active_sessions, pending_reviews)
        session_launcher: For launching the actual session
        session_restorer: For restoring orphaned terminals

    Returns:
        The launched session or None
    """
    result = session_launcher.launch_review_session(review, state.active_sessions)
    # Always remove from pending after attempting launch
    state.pending_reviews = [r for r in state.pending_reviews if r.pr_number != review.pr_number]
    if result.success and result.session:
        state.active_sessions.append(result.session)
    elif result.keep_queued:
        # Terminal exists but we can't track it - try to restore/adopt it
        session_name = f"review-{review.pr_number}"
        restored = session_restorer.restore_sessions(
            running=[{"issue_number": review.issue_number, "tab_name": session_name, "is_review": True}],
            already_tracked=state.active_sessions,
        )
        if restored:
            state.active_sessions.extend(restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
        else:
            logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None


def orchestrator_launch_rework_session(
    rework: PendingRework,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Orchestrator wrapper for launching rework sessions - moved per method table.

    Args:
        rework: The pending rework to launch
        state: Orchestrator state (active_sessions, pending_reworks)
        session_launcher: For launching the actual session
        session_restorer: For restoring orphaned terminals

    Returns:
        The launched session or None
    """
    result = session_launcher.launch_rework_session(rework, state.active_sessions)
    # Always remove from pending after attempting launch
    state.pending_reworks = [r for r in state.pending_reworks if r.issue_key != rework.issue_key]
    if result.success and result.session:
        state.active_sessions.append(result.session)
    elif result.keep_queued:
        # Terminal exists but we can't track it - try to restore/adopt it
        issue_number = rework.resolve_issue_number()
        if issue_number is None:
            logger.warning("[ORPHAN] Rework missing issue number: %s", rework.issue_key)
            return None
        session_name = f"rework-{issue_number}"
        restored = session_restorer.restore_sessions(
            running=[{"issue_number": issue_number, "tab_name": session_name, "is_review": False}],
            already_tracked=state.active_sessions,
        )
        if restored:
            state.active_sessions.extend(restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
        else:
            logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None


def process_active_sessions(
    state: "OrchestratorState",
    observer: "SessionObserver",
    session_controller: "SessionController",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
) -> None:
    """Process active sessions - moved from Orchestrator per method table.

    Args:
        state: Orchestrator state (active_sessions)
        observer: Session observer for checking session status
        session_controller: For deciding outcome
        completion_handler: For processing completion
        action_applier: For applying actions
        worktree_manager: For worktree cleanup
        kill_session_fn: Function to kill terminal session
        config: Configuration
    """
    import time
    from ..observation.observation import SessionObservation

    for session in list(state.active_sessions):
        session_start = time.monotonic()
        obs = observer.observe_session(session)
        if obs.observation == SessionObservation.RUNNING:
            continue
        decision = session_controller.decide_outcome(
            obs, session.worktree_path, session.issue.number,
            session.issue.title, session.terminal_id, session.completion_path,
            validation_retry_count=session.validation_retry_count
        )
        # Extract pr_url, errors, and diagnostic_path from completion processor result
        pr_url_hint = None
        processing_errors = None
        diagnostic_path = None
        validation_error = decision.validation_error
        validation_error_file = decision.validation_error_file
        if decision.processing_result:
            if decision.processing_result.pr_url:
                pr_url_hint = decision.processing_result.pr_url
            if decision.processing_result.errors:
                processing_errors = decision.processing_result.errors
            if decision.processing_result.diagnostic_path:
                diagnostic_path = decision.processing_result.diagnostic_path
        handle_session_completion(
            session, decision.status, state, completion_handler, action_applier,
            observer, worktree_manager, kill_session_fn, config,
            pr_url_hint=pr_url_hint, processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            validation_error=validation_error,
            validation_error_file=str(validation_error_file) if validation_error_file else None,
        )
        session_elapsed = time.monotonic() - session_start
        if session_elapsed > 5:
            logger.warning(
                "[LOOP] Session handling took %.1fs (session=%s issue=%s observation=%s)",
                session_elapsed,
                session.terminal_id,
                session.issue.number,
                obs.observation.value,
            )


def launch_triage_session(
    triage: PendingTriageReview,
    config: Config,
    launch_session_fn: Callable[[Issue], Optional[Session]],
) -> None:
    """Launch triage session - moved from Orchestrator per method table.

    Args:
        triage: The pending triage review
        config: Configuration with triage agent settings
        launch_session_fn: Function to launch issue sessions
    """
    agent = config.triage_review_agent
    if not agent or agent not in config.agents:
        raise ValueError(f"Invalid triage agent: {agent}")
    launch_session_fn(Issue(triage.issue_number, triage.title, [agent]))


def session_launcher_callback(
    session_type: "SessionType",
    number: int,
    launch_issue_fn: Callable[[int], Optional[Session]],
    launch_review_fn: Callable[[int], Optional[Session]],
    launch_rework_fn: Callable[[int], Optional[Session]],
    launch_triage_fn: Callable[[int], Optional[Session]],
) -> Optional[Session]:
    """Session launcher callback - moved per method table."""
    from .session_manager import SessionType
    handlers = {
        SessionType.ISSUE: launch_issue_fn,
        SessionType.REVIEW: launch_review_fn,
        SessionType.REWORK: launch_rework_fn,
        SessionType.TRIAGE: launch_triage_fn,
    }
    return handlers.get(session_type, lambda n: None)(number)


def restore_running_sessions(
    running: list["DiscoveredSession"],
    active_sessions: list[Session],
    session_restorer: "SessionRestorer",
) -> None:
    """Restore running sessions - moved per method table."""
    active_sessions.extend(session_restorer.restore_sessions(running, active_sessions))


def parse_session_ref(
    session_name: str,
    operation: str,
    events: EventSink,
):
    """Parse session ref - moved per method table."""
    from .session_manager import SessionRef
    try:
        return SessionRef.from_name(session_name)
    except ValueError as e:
        from ..events import EventName
        from ..ports import TraceEvent
        events.publish(TraceEvent(EventName.SESSION_NAME_PARSE_ERROR, {"session_name": session_name, "error": str(e)}))
        raise


def create_session(
    name: str,
    cmd: str,
    wd: Path,
    title: str | None,
    session_manager: SessionManager,
    events: EventSink,
) -> bool:
    """Create session - moved per method table."""
    from .session_manager import SessionContext
    ref = parse_session_ref(name, "create", events)
    return session_manager.start(SessionContext(ref=ref, command=cmd, working_dir=wd, title=title))


def session_exists(name: str, session_manager: SessionManager, events: EventSink) -> bool:
    """Check if session exists - moved per method table."""
    return session_manager.exists(parse_session_ref(name, "exists", events))


def kill_session(name: str, session_manager: SessionManager, events: EventSink) -> None:
    """Kill session - moved per method table."""
    session_manager.stop(parse_session_ref(name, "kill", events))


def get_session_machine(name: str, n: int, timeout: int, state_machines: "StateMachineManager") -> Optional["SessionStateMachine"]:
    """Get session state machine - moved per method table."""
    return state_machines.get_session_machine(name, n, timeout)


def orchestrator_launch_session(
    issue: IssueProtocol,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
) -> Optional[Session]:
    """Launch session wrapper - moved per method table.

    Args:
        issue: The issue to launch a session for
        state: Orchestrator state (active_sessions)
        session_launcher: For launching the actual session

    Returns:
        The launched session or None
    """
    result = session_launcher.launch_issue_session(issue, state.active_sessions)
    if result.success and result.session:
        state.active_sessions.append(result.session)
    return result.session if result.success else None
