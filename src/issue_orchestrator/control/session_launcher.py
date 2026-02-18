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
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional, Callable

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from .dependency_evaluator import DependencyEvaluator
    from .completion_handler import CompletionHandler
    from .completion_observer import CompletionObserver, ObservationDecision
    from .action_applier import ActionApplier
    from .session_manager import SessionType
    from .session_controller import SessionController
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager
    from ..observation.observer import SessionObserver
    from ..domain.models import OrchestratorState
    from ..ports.session_runner import DiscoveredSession
    from ..ports.claim_manager import ClaimManager
    from .provider_resilience import ProviderResilienceManager
    from .label_manager import LabelManager

from ..infra.config import Config
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import issue_log, log_context
from ..events import EventName
from ..domain.models import Issue, Session, SessionStatus, PendingReview, PendingRework, PendingTriageReview, get_completion_path, SessionKey, TaskKind, AgentConfig
from .worktree import WorktreePreparationError
from .worktree_context import WorktreeContext
from ..domain.triage_manifest import TriageManifest
from .triage_manifest_builder import TriageManifestBuilder
from ..ports import (
    ManifestDownloader,
    EventSink,
    TraceEvent,
    ReviewState,
    RepositoryHost,
    Issue as IssueProtocol,
    WorkingCopy,
    CommandRunner,
)
from ..ports.session_output import SessionOutput
from ..ports.worktree_manager import WorktreeManager, WorktreeReuseOptions, WorktreeInfo
from ..ports.session_log import detect_ai_system_from_command
from ..ports.provider_resilience import ProviderErrorType
from .provider_availability import ProviderAvailabilityPolicy
from .action_applier import ActionApplier
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .session_manager import SessionManager
from .transition_log import log_transition

logger = logging.getLogger(__name__)


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


@dataclass
class ClaimAcquisitionResult:
    """Result of attempting to acquire a distributed claim for an issue.

    Used to track claim state through the launch process so cleanup
    can release claims on failure.
    """

    success: bool
    lease_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None

    def as_launch_failure(self) -> LaunchResult:
        """Convert a failed claim to a LaunchResult."""
        return LaunchResult(None, False, self.error or "Claim acquisition failed")


class SessionLauncher:
    """Launches agent sessions for issues, reviews, and reworks.

    Dependencies:
    - config: Configuration with agent definitions
    - events: EventSink for trace events
    - repository_host: For GitHub reads during launch
    - action_applier: For applying label/comment mutations
    - session_manager: For terminal session operations
    - manifest_downloader: For downloading PR data in triage sessions
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
        manifest_downloader: ManifestDownloader,
        session_exists_fn: Callable[[str], bool],
        create_session_fn: Callable[[str, str, Path, str | None], bool],
        get_issue_machine: Callable[["IssueProtocol"], Optional["IssueStateMachine"]],
        get_session_machine: Callable[[str, int, int], Optional["SessionStateMachine"]],
        get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
        refresh_issue_fn: Optional[Callable[[int], Optional["IssueProtocol"]]] = None,
        dependency_evaluator: Optional["DependencyEvaluator"] = None,
        claim_manager: Optional["ClaimManager"] = None,
        provider_resilience: Optional["ProviderResilienceManager"] = None,
        remove_session_machine: Callable[[str], None] | None = None,
        label_manager: Optional["LabelManager"] = None,
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
        self._manifest_downloader = manifest_downloader
        self._session_exists = session_exists_fn
        self._create_session = create_session_fn
        self._get_issue_machine = get_issue_machine
        self._get_session_machine = get_session_machine
        self._get_review_machine = get_review_machine
        self._refresh_issue = refresh_issue_fn
        self._dependency_evaluator = dependency_evaluator
        self._claim_manager = claim_manager
        self._provider_resilience = provider_resilience
        self._provider_policy = ProviderAvailabilityPolicy(config, provider_resilience) if provider_resilience else None
        self._remove_session_machine = remove_session_machine
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

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

    def _build_session_env(
        self,
        *,
        completion_path: str,
        session_id: str,
        agent_label: str,
        issue_number: int,
        run_dir: Path,
    ) -> str:
        """Build the common env-export string for all session types.

        Includes the orchestrator venv on PATH so ``agent-done`` is always
        reachable — even when the target repo is a foreign (non-orchestrator)
        repository with no ``.venv``.

        NOTE: Validation config is NOT passed via env var.  ``agent-done``
        reads validation config from the worktree's config file so tests are
        deterministic (no env var leakage).
        """
        orch_bin = Path(sys.executable).parent
        return (
            f"export {ENV_PREFIX}COMPLETION_PATH='{completion_path}'"
            f" {ENV_PREFIX}SESSION_ID='{session_id}'"
            f" {ENV_PREFIX}AGENT_LABEL='{agent_label}'"
            f" {ENV_PREFIX}ISSUE_NUMBER='{issue_number}'"
            f" {ENV_PREFIX}API_PORT='{self.config.web_port}'"
            f" {ENV_PREFIX}VALIDATION_OUTPUT_DIR='{run_dir}'"
            f" {ENV_PREFIX}RUN_DIR='{run_dir}'"
            f' PATH="{orch_bin}:$PATH"'
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Phase helpers for launch_issue_session
    # These represent distinct phases a human would describe when explaining
    # the launch process. See .claude/skills/refactoring/SKILL.md
    # ─────────────────────────────────────────────────────────────────────────

    def _check_launch_preconditions(
        self,
        issue: "IssueProtocol",
        active_sessions: list[Session],
        session_name: str,
    ) -> LaunchResult | None:
        """Validate config and check for conflicts before launching.

        Returns LaunchResult on failure, None if preconditions pass.
        """
        if issue.agent_type is None:
            return LaunchResult(None, False, f"Issue #{issue.number} has no agent type label")

        if not self.config.agents.get(issue.agent_type):
            return LaunchResult(None, False, f"No agent config for {issue.agent_type}")

        if not self.config.repo:
            return LaunchResult(None, False, "No repo configured")

        if any(s.issue.number == issue.number for s in active_sessions):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")

        if self._session_exists(session_name):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running")

        return None

    def _verify_dependencies_fresh(self, issue: "IssueProtocol") -> LaunchResult | None:
        """CAS check: verify dependencies haven't changed since scheduling.

        Returns LaunchResult on failure, None if dependencies still satisfied.
        """
        if not self._dependency_evaluator or not self._refresh_issue:
            return None

        fresh_issue = self._refresh_issue(issue.number)
        if not fresh_issue or not fresh_issue.body:
            return None

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

        return None

    def _acquire_issue_claim(self, issue: "IssueProtocol") -> ClaimAcquisitionResult:
        """Acquire distributed claim for an issue if claim manager is configured.

        Handles the claim attempt and convergence check. On success, returns
        claim info for passing to Session. On failure, returns error for
        early exit.
        """
        if not self._claim_manager:
            return ClaimAcquisitionResult(success=True)  # No claim needed

        logger.info(issue_log(issue.number, "Acquiring claim..."))
        claim_result = self._claim_manager.attempt_claim(issue.number)

        if not claim_result.success:
            log_transition(
                "issue", issue.number, "LAUNCHING", "CLAIM_FAILED",
                f"claim attempt failed: {claim_result.error}"
            )
            self.events.publish(TraceEvent(
                EventName.CLAIM_CONTESTED,
                {
                    "issue_number": issue.number,
                    "issue_title": issue.title,
                    "error": claim_result.error,
                },
            ))
            return ClaimAcquisitionResult(
                success=False,
                error=f"Failed to claim issue: {claim_result.error}"
            )

        # Run convergence to confirm ownership
        logger.info(issue_log(issue.number, "Running claim convergence..."))
        converged = self._claim_manager.run_convergence(issue.number, claim_result.lease_id or "")

        if not converged:
            log_transition(
                "issue", issue.number, "LAUNCHING", "CLAIM_LOST",
                "convergence failed - another claimant won"
            )
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
            return ClaimAcquisitionResult(
                success=False,
                error="Claim convergence failed - another orchestrator won"
            )

        # Claim acquired successfully
        lease_seconds = self.config.claim_lease_seconds if hasattr(self.config, 'claim_lease_seconds') else 900
        acquired_at = datetime.now()
        logger.info(issue_log(issue.number, "Claim acquired: lease_id=%s"), claim_result.lease_id)
        self.events.publish(TraceEvent(
            EventName.CLAIM_ACQUIRED,
            {
                "issue_number": issue.number,
                "lease_id": claim_result.lease_id,
            },
        ))
        return ClaimAcquisitionResult(
            success=True,
            lease_id=claim_result.lease_id,
            lease_acquired_at=acquired_at,
            lease_expires_at=acquired_at + timedelta(seconds=lease_seconds),
        )

    def _release_claim_if_held(self, issue_number: int, claim: ClaimAcquisitionResult) -> None:
        """Release claim if one was acquired. Used for cleanup on failure."""
        if self._claim_manager and claim.lease_id:
            self._claim_manager.release_claim(issue_number, claim.lease_id)
            logger.info(issue_log(issue_number, "Released claim: lease_id=%s"), claim.lease_id)

    def _is_triage_session(self, agent_type: str | None) -> bool:
        """Check if this agent type is the triage review agent."""
        return bool(
            self.config.triage_review_agent
            and agent_type == self.config.triage_review_agent
        )

    def _prepare_triage_manifest(
        self,
        worktree_path: Path,
        run_dir: Path,
    ) -> TriageManifest | None:
        """Build and download triage manifest for a triage session.

        Creates a manifest listing PRs that need triage review, downloads
        their diffs and metadata to the session directory.

        Args:
            worktree_path: Path to the worktree
            run_dir: Path to the session run directory

        Returns:
            The populated manifest, or None if no PRs need triage
        """
        # Build manifest with PRs needing triage
        builder = TriageManifestBuilder(
            repository_host=self.repository_host,
            code_reviewed_label=self.config.code_reviewed_label or "code-reviewed",
            triage_reviewed_label=self.config.triage_reviewed_label or "triage-reviewed",
            triage_failed_label=self.config.triage_failed_label or "triage-failed",
        )

        # Data goes in session run directory
        data_dir = f".issue-orchestrator/sessions/{run_dir.name}/triage-data"
        manifest = builder.build(data_dir)

        if not manifest.prs:
            logger.info("[triage] No PRs need triage review")
            return None

        # Download diffs and metadata via injected port
        manifest = self._manifest_downloader.download(manifest, worktree_path)

        # Write manifest to session directory
        manifest_path = worktree_path / data_dir / "manifest.json"
        manifest.write(manifest_path)

        logger.info(
            "[triage] Prepared manifest with %d PRs: %s",
            len(manifest.prs),
            manifest_path,
        )

        return manifest

    def launch_issue_session(  # noqa: C901, PLR0912 - coordinator with claim acquisition, worktree setup, and error handling phases
        self,
        issue: "IssueProtocol",
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a session for an issue.

        This is a coordinator function that orchestrates the multi-step launch process.
        Meaningful phases are extracted as helpers (_check_launch_preconditions,
        _verify_dependencies_fresh, _acquire_issue_claim). Remaining complexity is
        error handling for worktree/label/session failures - these belong inline
        with their operations rather than scattered across separate functions.

        Args:
            issue: The issue to work on
            active_sessions: Current active sessions (for conflict detection)

        Returns:
            LaunchResult with session if successful
        """
        launch_start = time.time()
        session_name = f"issue-{issue.number}"
        logger.info(issue_log(issue.number, "Session starting: type=code title=%s"), issue.title)

        # Phase 1: Validate preconditions
        if result := self._check_launch_preconditions(issue, active_sessions, session_name):
            return result

        # Safe to access after precondition check - issue.agent_type and agent_config
        # are guaranteed non-None by _check_launch_preconditions
        assert issue.agent_type is not None  # Validated in preconditions
        agent_config = self.config.agents.get(issue.agent_type)
        assert agent_config is not None  # Validated in preconditions
        issue_key = issue.key
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)

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

        # Phase 2: Verify dependencies haven't changed (CAS check)
        if result := self._verify_dependencies_fresh(issue):
            return result

        # Provider circuit breaker check
        if result := self._check_provider_circuit(agent_config.provider, issue.number):
            return result

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Phase 3: Acquire distributed claim
        claim = self._acquire_issue_claim(issue)
        if not claim.success:
            return claim.as_launch_failure()

        # Phase 4: Prepare worktree
        step_start = time.time()
        logger.info(issue_log(issue.number, "Creating worktree..."))
        phase_name = "coding-1"  # Initial coding session is always attempt 1
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
            phase_name=phase_name,
        )

        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self._lm.needs_human
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
            self._release_claim_if_held(issue.number, claim)
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

        # For triage sessions, prepare manifest with PRs to review
        triage_manifest: TriageManifest | None = None
        if self._is_triage_session(issue.agent_type):
            triage_manifest = self._prepare_triage_manifest(worktree_path, run.run_dir)
            if triage_manifest:
                # Store manifest path in session for completion handling
                ctx.update_manifest({"triage_manifest": str(run.run_dir / "triage-data" / "manifest.json")})

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
        in_progress_label = self._lm.in_progress
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
            self._release_claim_if_held(issue.number, claim)
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
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(issue.agent_type, session_name=ctx.phase_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=issue.agent_type,
            issue_number=issue.number,
            run_dir=run.run_dir,
        )
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
                    label=self._lm.in_progress,
                    reason="session creation failed",
                ),
            ], context="launch_session_creation_failed")
            self._release_claim_if_held(issue.number, claim)
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
            lease_id=claim.lease_id,
            lease_acquired_at=claim.lease_acquired_at,
            lease_expires_at=claim.lease_expires_at,
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
            "agent": issue.agent_type,
            "task": "code",
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
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

        if result := self._check_provider_circuit(agent_config.provider, review.issue_number):
            return result

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

        # Determine review attempt number from rework_count
        # First review is review-1, after first rework it's review-2, etc.
        review_machine = self._get_review_machine(review.pr_number, review.issue_number)
        rework_count = review_machine.rework_count if review_machine else 0
        review_attempt = rework_count + 1
        phase_name = f"review-{review_attempt}"

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
            phase_name=phase_name,
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("review", review.pr_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(review.issue_number, "BLOCKED: worktree preparation failed for review: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self._lm.needs_human
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

        existing_work = self._build_review_existing_work(worktree_info, review.pr_number)

        # Build command
        base_command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(agent_label, session_name=ctx.phase_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=agent_label,
            issue_number=review.issue_number,
            run_dir=run.run_dir,
        )
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
            pr_number=review.pr_number,
            rework_cycle=rework_count if rework_count > 0 else None,
        )

        log_transition("review", review.pr_number, "LAUNCHING", "ACTIVE", "session launched")

        # Emit event
        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(TraceEvent(EventName.REVIEW_STARTED, {
            "pr_number": review.pr_number,
            "issue_number": review.issue_number,
            "agent": agent_label,
            "task": "review",
            "session_name": session_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # State machine transition
        self._trigger_review_state_transition(review.pr_number, review.issue_number)

        return LaunchResult(session, True)

    def _build_review_existing_work(
        self,
        worktree_info: WorktreeInfo,
        pr_number: int,
    ) -> str | None:
        existing_work: str | None = None
        if worktree_info.rebase_failed:
            existing_work = (
                "WARNING: This PR branch could not be rebased onto main due to merge conflicts. "
                "The branch is behind main. When reviewing, consider whether merge conflicts "
                "need to be resolved before the PR can be merged."
            )
            logger.warning("[launch] Rebase failed for review - PR branch is behind main")

        pr_info = self.repository_host.get_pr(pr_number)
        if not pr_info:
            return existing_work

        keep_current_label = self._lm.review_keep_approach
        if keep_current_label not in pr_info.labels:
            return existing_work

        keep_current_note = (
            f"REVIEWER INSTRUCTION: This PR is labeled '{keep_current_label}'. "
            "Keep the current approach. Do not propose alternative approaches unless "
            "the current approach cannot work or violates correctness, safety, or security. "
            "If the current approach is invalid, fail the review with a brief note."
        )
        if existing_work:
            return f"{existing_work}\n\n{keep_current_note}"
        return keep_current_note

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

        if result := self._check_provider_circuit(agent_config.provider, issue_number):
            return result

        pr_number, branch_name = self._resolve_rework_pr_details(issue_number)

        # Check for conflicts
        session_name = f"rework-{issue_number}"
        if result := self._check_rework_conflicts(session_name, active_sessions, issue_number):
            return result

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

        # Rework cycle N means coding attempt N+1
        # (initial coding was attempt 1, first rework is attempt 2, etc.)
        coding_attempt = rework.rework_cycle + 1
        phase_name = f"coding-{coding_attempt}"

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
            phase_name=phase_name,
        )

        # Handle worktree preparation errors
        if ctx.error:
            log_transition("rework", issue_number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue_number, "BLOCKED: worktree preparation failed for rework: %s"), ctx.error)
            _write_worktree_diagnostic(ctx.error)
            needs_human_label = self._lm.needs_human
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

        # Copy reviewer feedback from review session to rework session (for local cache)
        self._copy_review_feedback_to_rework(worktree_path, pr_number, run.run_dir)

        # Add reviewer feedback to the prompt so agent knows what to fix
        # Checks local file first (within cache window), falls back to GitHub API
        reviewer_feedback = self._format_reviewer_feedback(pr_number, run_dir=run.run_dir)
        if reviewer_feedback:
            if existing_work:
                existing_work = f"{existing_work}\n\n{reviewer_feedback}"
            else:
                existing_work = reviewer_feedback
            logger.info("[launch] Including reviewer feedback in rework session prompt")

            # Save feedback for diagnostics (per-cycle file)
            self._session_output.save_review_feedback(
                worktree_path=worktree_path,
                cycle=rework.rework_cycle,
                feedback=reviewer_feedback,
                pr_number=pr_number,
            )

        # Build command
        base_command = agent_config.get_command(
            issue_number=issue_number,
            issue_title=f"Rework PR #{pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=pr_number,
            existing_work=existing_work,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(rework.agent_type, session_name=ctx.phase_name)
        self._session_output.update_manifest(
            run.run_dir,
            {"completion_path": completion_path},
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=rework.agent_type,
            issue_number=issue_number,
            run_dir=run.run_dir,
        )
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
            pr_number=pr_number,
            rework_cycle=rework.rework_cycle,
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
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
        }))

        # Update rework cycle label
        self._update_rework_cycle_label(pr_number, issue_number, rework.rework_cycle)

        # Remove needs-rework label
        self._apply_actions([
            RemoveLabelAction(
                issue_number=pr_number,
                label=self._lm.needs_rework,
                reason="rework started",
            ),
        ], context="rework_remove_needs_rework")
        self.events.publish(TraceEvent(EventName.PR_VIEW_CHANGED, {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "issue_key": str(issue_number),
            "removed": [self._lm.needs_rework],
        }))

        return LaunchResult(session, True)

    def _find_review_feedback_file(
        self,
        worktree_path: Path,
        pr_number: int,
    ) -> Path | None:
        """Find the reviewer feedback file from the most recent review session.

        Looks for review-{pr_number}__* directories and returns the path to
        reviewer-feedback.json from the most recent one (if it exists).

        Args:
            worktree_path: Path to the worktree.
            pr_number: The PR number to find feedback for.

        Returns:
            Path to the feedback file, or None if not found.
        """
        sessions_dir = worktree_path / ".issue-orchestrator" / "sessions"
        if not sessions_dir.exists():
            return None

        # Find all review session directories for this PR
        review_prefix = f"review-{pr_number}__"
        review_dirs = sorted(
            [d for d in sessions_dir.iterdir() if d.is_dir() and d.name.startswith(review_prefix)],
            key=lambda d: d.name,
            reverse=True,  # Most recent first (timestamp suffix)
        )

        for review_dir in review_dirs:
            feedback_file = review_dir / "reviewer-feedback.json"
            if feedback_file.exists():
                return feedback_file

        return None

    def _copy_review_feedback_to_rework(
        self,
        worktree_path: Path,
        pr_number: int,
        rework_run_dir: Path,
    ) -> Path | None:
        """Copy reviewer feedback from review session to rework session.

        Finds the most recent review session's feedback file and copies it
        to the rework session's run directory.

        Args:
            worktree_path: Path to the worktree.
            pr_number: The PR number to find feedback for.
            rework_run_dir: Path to the rework session's run directory.

        Returns:
            Path to the copied feedback file, or None if not found/failed.
        """
        source_file = self._find_review_feedback_file(worktree_path, pr_number)
        if not source_file:
            logger.debug(
                "[launch] No review feedback file found for PR #%s in worktree %s",
                pr_number, worktree_path
            )
            return None

        dest_file = rework_run_dir / "reviewer-feedback.json"
        try:
            import shutil
            shutil.copy2(source_file, dest_file)
            logger.info(
                "[launch] Copied reviewer feedback for PR #%s: %s -> %s",
                pr_number, source_file, dest_file
            )
            return dest_file
        except Exception as e:
            logger.warning(
                "[launch] Failed to copy reviewer feedback for PR #%s: %s",
                pr_number, e
            )
            return None

    def _read_local_reviewer_feedback(self, run_dir: Path) -> str | None:
        """Read reviewer feedback from local file if within cache window.

        Args:
            run_dir: Path to the session's run directory.

        Returns:
            The review_issues text if found and within cache window, None otherwise.
        """
        feedback_file = run_dir / "reviewer-feedback.json"
        if not feedback_file.exists():
            return None

        try:
            import json
            from datetime import datetime, timezone
            data = json.loads(feedback_file.read_text())
            timestamp_str = data.get("timestamp")
            review_issues = data.get("review_issues")

            if not timestamp_str or not review_issues:
                return None

            # Check if within cache window
            cache_minutes = self.config.reviewer_feedback_cache_minutes
            if cache_minutes < 0:
                # Cache disabled
                return None

            feedback_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            age_minutes = (datetime.now(timezone.utc) - feedback_time).total_seconds() / 60

            if age_minutes <= cache_minutes:
                logger.info(
                    "[launch] Using local reviewer feedback (age: %.1f min, cache window: %d min)",
                    age_minutes, cache_minutes
                )
                return review_issues
            else:
                logger.debug(
                    "[launch] Local feedback too old (age: %.1f min, cache window: %d min), will fetch from GitHub",
                    age_minutes, cache_minutes
                )
                return None

        except Exception as e:
            logger.warning("[launch] Failed to read local reviewer feedback: %s", e)
            return None

    def _format_reviewer_feedback(
        self,
        pr_number: int,
        run_dir: Path | None = None,
    ) -> str | None:
        """Extract and format reviewer feedback for rework prompt.

        First checks for local feedback file (if run_dir provided and within
        cache window), then falls back to fetching from GitHub API.

        Uses retry with backoff when fetching from GitHub to handle eventual
        consistency - since we're in a rework flow, we expect reviews to exist.

        Args:
            pr_number: The PR number to get reviews for.
            run_dir: Optional path to the session's run directory for local cache.

        Returns:
            Formatted feedback string, or None if no actionable feedback found.
        """
        # Try local file first (if run_dir provided)
        if run_dir:
            local_feedback = self._read_local_reviewer_feedback(run_dir)
            if local_feedback:
                # Format local feedback
                return f"REVIEWER FEEDBACK (address these issues):\n\n{local_feedback}"

        # Fall back to GitHub API with retry for eventual consistency
        backoff_delays = [1.0, 2.0, 4.0]
        feedback_reviews = []

        for attempt, delay in enumerate(backoff_delays):
            try:
                reviews = self.repository_host.get_pr_reviews(pr_number)
            except Exception as e:
                logger.warning("Failed to fetch PR reviews for PR #%s: %s", pr_number, e)
                return None

            # Filter to reviews with actionable feedback
            feedback_reviews = [
                r for r in reviews
                if r.get("state") in (ReviewState.CHANGES_REQUESTED.value, ReviewState.COMMENTED.value)
                and r.get("body", "").strip()
            ]

            if feedback_reviews:
                if attempt > 0:
                    logger.info(
                        "[launch] Found reviewer feedback after %d retry attempt(s) for PR #%s",
                        attempt, pr_number
                    )
                break

            # No feedback found yet - wait and retry for eventual consistency
            if attempt < len(backoff_delays) - 1:
                logger.debug(
                    "[launch] No reviewer feedback found for PR #%s, retrying in %.1fs (attempt %d/%d)",
                    pr_number, delay, attempt + 1, len(backoff_delays)
                )
                time.sleep(delay)

        if not feedback_reviews:
            logger.info(
                "[launch] No reviewer feedback found for PR #%s after %d attempts",
                pr_number, len(backoff_delays)
            )
            return None

        lines = ["REVIEWER FEEDBACK (address these issues):"]
        for review in feedback_reviews:
            reviewer = review.get("user", {}).get("login", "reviewer")
            state = review.get("state", "")
            body = review.get("body", "").strip()
            lines.append(f"\n[{reviewer} - {state}]")
            lines.append(body)

        return "\n".join(lines)

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

    def _wrap_provider_command(self, base_command: str, agent_config: "AgentConfig", run_dir: Path) -> str:
        """Wrap provider command with retry/circuit reporting."""
        retry_cfg = self.config.provider_resilience.short_retry
        provider = agent_config.provider or detect_ai_system_from_command(base_command)
        cmd = [
            sys.executable,
            "-m",
            "issue_orchestrator.entrypoints.cli_tools.provider_runner",
            "--command",
            base_command,
            "--timeout-seconds",
            str(agent_config.timeout_minutes * 60),
            "--max-attempts",
            str(retry_cfg.max_attempts),
            "--initial-backoff-seconds",
            str(retry_cfg.initial_backoff_seconds),
            "--max-backoff-seconds",
            str(retry_cfg.max_backoff_seconds),
            "--run-dir",
            str(run_dir),
        ]
        if retry_cfg.jitter:
            cmd.append("--jitter")
        else:
            cmd.append("--no-jitter")
        if provider:
            cmd.extend(["--provider", provider])
        return shlex.join(cmd)

    def _check_provider_circuit(self, provider: str | None, issue_number: int) -> Optional["LaunchResult"]:
        if not provider or not self._provider_policy:
            return None
        if not self._provider_policy.is_open(provider):
            return None
        blocked_label = self._provider_policy.blocked_label()
        self._apply_actions([
            AddLabelAction(
                issue_number=issue_number,
                label=blocked_label,
                reason=f"provider unavailable: {provider}",
            ),
        ], context="provider_unavailable")
        return LaunchResult(None, False, f"Provider unavailable: {provider}")

    def _check_rework_conflicts(
        self,
        session_name: str,
        active_sessions: list[Session],
        issue_number: int,
    ) -> Optional["LaunchResult"]:
        if any(s.terminal_id == session_name for s in active_sessions):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "already in active_sessions")
            return LaunchResult(None, False, "Already in active sessions")
        if self._session_exists(session_name):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "terminal session already running")
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)
        return None

    def _resolve_rework_pr_details(self, issue_number: int) -> tuple[int, str]:
        prs = self.repository_host.get_prs_for_issue(issue_number)
        if not prs:
            return issue_number, f"{issue_number}-rework"
        pr = prs[0]
        return pr.number, pr.branch

    def _trigger_issue_session_state_transitions(
        self,
        issue: "IssueProtocol",
        session_name: str,
        timeout_minutes: int,
    ) -> None:
        """Trigger state machine transitions for issue session launch."""
        from ..domain.state_machines.issue_machine import IssueState
        from ..domain.state_machines.session_machine import SessionState

        logger.debug(f"[STATE_MACHINE] Triggering transitions for issue #{issue.number}")
        issue_machine = self._get_issue_machine(issue)
        if issue_machine.state == IssueState.AVAILABLE.value:
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: AVAILABLE -> CLAIMED")
            issue_machine.claim()
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: CLAIMED -> IN_PROGRESS")
            issue_machine.start()

        session_machine = self._get_session_machine(session_name, issue.number, timeout_minutes)
        if session_machine.state != SessionState.PENDING.value:
            logger.warning(
                "[STATE_MACHINE] Session %s unexpected state %s during launch; resetting",
                session_name,
                session_machine.state,
            )
            if self._remove_session_machine is not None:
                self._remove_session_machine(session_name)
                session_machine = self._get_session_machine(session_name, issue.number, timeout_minutes)
            else:
                return

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
            label = self._lm.rework_cycle(i)
            removed.append(label)
            actions.append(RemoveLabelAction(
                issue_number=pr_number,
                label=label,
                reason="rework cycle update",
            ))
        added_label = self._lm.rework_cycle(cycle)
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


def _run_session_analysis(run_dir: Path) -> None:
    """Run the session analyzer and write analysis.json (best-effort)."""
    from .session_analyzer import analyze, write_analysis
    from ..domain.run_manifest import RunManifest

    try:
        manifest = RunManifest.load(run_dir)
        analysis = analyze(manifest)
        write_analysis(run_dir, analysis)
        logger.info("[ANALYSIS] %s — %s", run_dir.name, analysis.headline[:80])
    except FileNotFoundError:
        logger.debug("[ANALYSIS] No manifest in %s — skipping analysis", run_dir.name)
    except Exception:
        logger.warning("[ANALYSIS] Failed to analyze %s", run_dir.name, exc_info=True)


def handle_session_completion(  # noqa: C901, PLR0912 - handles validation, actions, observer cleanup, claims, and history
    session: Session,
    status: SessionStatus,
    state: "OrchestratorState",
    completion_handler: "CompletionHandler",
    action_applier: "ActionApplier",
    observer: "SessionObserver",
    worktree_manager: Optional[WorktreeManager],
    kill_session_fn: Callable[[str], None],
    config: Config,
    session_output: SessionOutput,
    pr_url_hint: Optional[str] = None,
    processing_errors: Optional[list[str]] = None,
    diagnostic_path: Optional[str] = None,
    validation_error: Optional[str] = None,
    validation_error_file: Optional[str] = None,
    review_exchange_completed: bool = False,
    review_exchange_halted: bool = False,
    blocked_label: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    completion_detail: Optional[dict[str, Any]] = None,
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
) -> None:
    """Handle session completion - moved from Orchestrator per method table.

    Complexity is inherent - this processes validation retries, completion,
    actions, observer cleanup, claim release, history, and failure tracking.
    These are sequential steps that share the session context.

    Args:
        session: The completed session
        status: The session status
        state: Orchestrator state (active_sessions, session_history, etc.)
        completion_handler: For processing completion
        action_applier: For applying actions
        observer: For cleanup
        worktree_manager: For worktree removal
        kill_session_fn: Function to kill terminal session
        session_output: For session artifact management
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
        completion_handler.mark_session_retry(session, reason="validation_retry")
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
        processing_errors=processing_errors,
        diagnostic_path=diagnostic_path,
        review_exchange_completed=review_exchange_completed,
        review_exchange_halted=review_exchange_halted,
        blocked_label=blocked_label,
        blocked_reason=blocked_reason,
        completion_detail=completion_detail,
    )
    if session.worktree_path:
        run_dir = session_output.find_run_dir(session.worktree_path, session.terminal_id)
        if run_dir:
            session_output.attach_claude_log(run_dir)
            _run_session_analysis(run_dir)
        else:
            logger.warning(
                "[%s] No session output dir found - Claude log won't be attached",
                session.terminal_id,
            )

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
        _append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        # Terminal exists but we can't track it - try to restore/adopt it
        session_name = f"review-{review.pr_number}"
        restored = session_restorer.restore_sessions(
            running=[{"issue_number": review.issue_number, "tab_name": session_name, "is_review": True}],
            already_tracked=state.active_sessions,
        )
        if restored:
            _append_unique_active_sessions(state.active_sessions, restored)
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
        _append_unique_active_sessions(state.active_sessions, [result.session])
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
            _append_unique_active_sessions(state.active_sessions, restored)
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

    DEPRECATED: Use observe_active_sessions() for the new async completion flow.
    This function is kept for backwards compatibility during migration.

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
        review_exchange_completed = False
        review_exchange_halted = False
        if decision.processing_result:
            if decision.processing_result.pr_url:
                pr_url_hint = decision.processing_result.pr_url
            if decision.processing_result.errors:
                processing_errors = decision.processing_result.errors
            if decision.processing_result.diagnostic_path:
                diagnostic_path = decision.processing_result.diagnostic_path
            review_exchange_completed = decision.processing_result.review_exchange_completed
            review_exchange_halted = decision.processing_result.review_exchange_halted
        handle_session_completion(
            session, decision.status, state, completion_handler, action_applier,
            observer, worktree_manager, kill_session_fn, config,
            session_output=session_controller.session_output,
            pr_url_hint=pr_url_hint, processing_errors=processing_errors,
            diagnostic_path=diagnostic_path,
            validation_error=validation_error,
            validation_error_file=str(validation_error_file) if validation_error_file else None,
            review_exchange_completed=review_exchange_completed,
            review_exchange_halted=review_exchange_halted,
            blocked_label=decision.blocked_label,
            blocked_reason=decision.blocked_reason,
            completion_detail=decision.completion_detail,
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


def _log_observation(session: Session, decision: "ObservationDecision") -> None:
    logger.info(
        "[OBSERVE] Session completed: session=%s issue=%d status=%s has_completion=%s",
        session.terminal_id,
        session.issue.number,
        decision.status.value,
        decision.observed is not None,
    )


def _publish_observation_event(
    session: Session,
    decision: "ObservationDecision",
    events: Optional[EventSink],
) -> None:
    if not events:
        return
    events.publish(TraceEvent(EventName.OBSERVATION_RESULT, {
        "issue_number": session.issue.number,
        "session_name": session.terminal_id,
        "status": decision.status.value,
        "has_completion": decision.observed is not None,
        "recovered_from_timeout": decision.recovered_from_timeout,
    }))


def _remove_active_session(state: "OrchestratorState", session: Session) -> None:
    state.active_sessions = [s for s in state.active_sessions if s.terminal_id != session.terminal_id]


def _kill_session(kill_session_fn: Callable[[str], None], session: Session) -> None:
    try:
        kill_session_fn(session.terminal_id)
        logger.debug("[OBSERVE] Killed terminal: %s", session.terminal_id)
    except Exception as exc:
        logger.warning("[OBSERVE] Failed to kill terminal %s: %s", session.terminal_id, exc)


def _release_claim_if_needed(
    session: Session,
    decision: "ObservationDecision",
    claim_manager: Optional["ClaimManager"],
    events: Optional[EventSink],
) -> None:
    if not claim_manager or not session.lease_id:
        return
    try:
        claim_manager.release_claim(session.issue.number, session.lease_id)
        logger.info(
            "[OBSERVE] Released claim for issue #%d: lease_id=%s",
            session.issue.number,
            session.lease_id,
        )
        if events:
            events.publish(TraceEvent(
                EventName.CLAIM_RELEASED,
                {
                    "issue_number": session.issue.number,
                    "lease_id": session.lease_id,
                    "status": decision.status.value,
                },
            ))
    except Exception as exc:
        logger.warning(
            "[OBSERVE] Failed to release claim for issue #%d: %s",
            session.issue.number,
            exc,
        )


def _update_provider_resilience(
    decision: "ObservationDecision",
    provider_resilience: Optional["ProviderResilienceManager"],
) -> None:
    if not provider_resilience or not decision.provider_status:
        return
    provider = decision.provider_status.provider
    if decision.provider_status.succeeded:
        provider_resilience.record_success(provider)
        return
    if decision.provider_status.error_type == ProviderErrorType.TRANSIENT:
        provider_resilience.record_transient_failure(
            provider,
            error_summary=decision.provider_status.last_error_summary,
            attempts=decision.provider_status.attempts,
        )


def _record_observed_completion(
    state: "OrchestratorState",
    session: Session,
    decision: "ObservationDecision",
) -> None:
    if decision.observed:
        state.observed_completions.append(decision.observed)
        logger.info(
            "[OBSERVE] Collected completion: issue=%d outcome=%s needs_publish=%s",
            session.issue.number,
            decision.observed.outcome,
            decision.observed.needs_publish,
        )
        return
    from ..domain.models import DiscoveredFailure
    state.discovered_failures.append(DiscoveredFailure(
        session.issue.number,
        session.issue.title,
        decision.status.value,
    ))
    state.failed_this_cycle.add(session.issue.number)
    logger.warning(
        "[OBSERVE] No completion record for issue #%d, status=%s",
        session.issue.number,
        decision.status.value,
    )


def _warn_if_slow(obs_elapsed: float, session: Session) -> None:
    if obs_elapsed <= 1.0:
        return
    logger.warning(
        "[OBSERVE] Session observation took %.1fs (session=%s issue=%s) - should be <1s",
        obs_elapsed,
        session.terminal_id,
        session.issue.number,
    )


def _append_unique_active_sessions(
    active_sessions: list[Session],
    incoming: list[Session],
) -> int:
    """Append sessions while preserving unique terminal identity."""
    existing_ids = {s.terminal_id for s in active_sessions}
    added = 0
    for session in incoming:
        if session.terminal_id in existing_ids:
            logger.warning(
                "[ACTIVE_SESSIONS] Duplicate terminal suppressed: %s (issue=%s)",
                session.terminal_id,
                session.issue.number,
            )
            continue
        active_sessions.append(session)
        existing_ids.add(session.terminal_id)
        added += 1
    return added


def _observe_active_session(
    state: "OrchestratorState",
    session: Session,
    observer: "SessionObserver",
    completion_observer: "CompletionObserver",
    kill_session_fn: Callable[[str], None],
    claim_manager: Optional["ClaimManager"],
    events: Optional[EventSink],
    provider_resilience: Optional["ProviderResilienceManager"],
) -> None:
    import time
    from ..observation.observation import SessionObservation

    obs_start = time.monotonic()
    obs = observer.observe_session(session)
    if obs.observation == SessionObservation.RUNNING:
        return

    decision = completion_observer.observe_completion(session, obs)

    _log_observation(session, decision)
    _publish_observation_event(session, decision, events)
    _remove_active_session(state, session)
    _kill_session(kill_session_fn, session)
    _release_claim_if_needed(session, decision, claim_manager, events)
    _update_provider_resilience(decision, provider_resilience)
    _record_observed_completion(state, session, decision)

    obs_elapsed = time.monotonic() - obs_start
    _warn_if_slow(obs_elapsed, session)


def observe_active_sessions(
    state: "OrchestratorState",
    observer: "SessionObserver",
    completion_observer: "CompletionObserver",
    kill_session_fn: Callable[[str], None],
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
    provider_resilience: Optional["ProviderResilienceManager"] = None,
) -> None:
    """Observe active sessions and collect completion facts (fast, no I/O-heavy operations).

    This is Phase 1 of the async completion flow:
    1. Observe each session to detect termination
    2. For terminated sessions, use CompletionObserver to read completion.json
    3. Collect ObservedCompletion facts into state.observed_completions
    4. Remove sessions from active tracking and kill terminals

    The Planner will see observed_completions and:
    - Plan immediate label updates (remove in-progress, add pr-pending/blocked)
    - Create PublishJobs for background execution

    Args:
        state: Orchestrator state (active_sessions, observed_completions)
        observer: Session observer for checking session status
        completion_observer: For reading completion.json (no execution)
        kill_session_fn: Function to kill terminal session
        claim_manager: Optional ClaimManager for releasing claims
        events: Optional EventSink for emitting events
    """
    for session in list(state.active_sessions):
        _observe_active_session(
            state=state,
            session=session,
            observer=observer,
            completion_observer=completion_observer,
            kill_session_fn=kill_session_fn,
            claim_manager=claim_manager,
            events=events,
            provider_resilience=provider_resilience,
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
    restored = session_restorer.restore_sessions(running, active_sessions)
    _append_unique_active_sessions(active_sessions, restored)


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
        _append_unique_active_sessions(state.active_sessions, [result.session])
    return result.session if result.success else None
