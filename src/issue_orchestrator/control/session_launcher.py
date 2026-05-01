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
import shlex
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Callable, Sequence

if TYPE_CHECKING:
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from .dependency_evaluator import DependencyEvaluator
    from .action_applier import ActionApplier
    from ..ports.claim_manager import ClaimManager
    from .provider_resilience import ProviderResilienceManager
    from .label_manager import LabelManager

from ..infra.config import Config
from ..infra.env import ENV_PREFIX
from ..infra.logging_config import issue_log, log_context
from ..events import EventName
from ..domain.models import (
    AgentConfig,
    Issue,
    PendingReview,
    PendingRework,
    PendingValidationRetry,
    Session,
    SessionKey,
    TaskKind,
    get_completion_path,
)
from .worktree_context import WorktreeContext
from ..infra.validation_state import DEFAULT_RETRY_TEMPLATE
from ..domain.triage_manifest import TriageManifest
from .triage_manifest_builder import TriageManifestBuilder
from ..ports import (
    ManifestDownloader,
    EventSink,
    RepositoryHost,
    Issue as IssueProtocol,
    WorkingCopy,
    CommandRunner,
)
from ..ports.session_output import SessionOutput
from ..ports.event_sink import SessionStartedEventPayload, make_session_started_event
from ..ports.worktree_manager import WorktreeManager, WorktreeReuseOptions
from ..ports.session_log import detect_ai_system_from_command
from ..ports.event_sink import make_run_scoped_event, make_trace_event
from .provider_availability import ProviderAvailabilityPolicy
from .action_applier import ActionApplier
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .session_manager import SessionManager
from .session_launch_types import ClaimAcquisitionResult, LaunchResult
from .session_rework_launcher import (
    ReworkLaunchDependencies,
    launch_rework_session as launch_rework_flow,
)
from .session_review_support import (
    build_review_existing_work,
    review_launch_validity,
)
from .session_worktree_diagnostics import (
    build_worktree_error_comment,
    write_worktree_diagnostic,
)
from .transition_log import log_transition
from .isolation import build_runtime_tool_env, build_runtime_tool_env_assignments

logger = logging.getLogger(__name__)


def detect_existing_work(
    worktree_path: Path,
    working_copy: WorkingCopy,
    *,
    seed_ref: str | None = None,
) -> Optional[str]:
    """Check if worktree has commits ahead of main and return context for agent."""
    try:
        if seed_ref:
            head_sha = working_copy.get_head_sha(worktree_path)
            if head_sha and head_sha == seed_ref:
                return None

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


def _truncate_with_tail(text: str, max_length: int = 4000, tail_length: int = 2000) -> str:
    """Truncate long validation output while preserving the summary tail."""
    if len(text) <= max_length:
        return text
    head_length = max_length - tail_length - 30
    if head_length < 100:
        return f"[...truncated {len(text) - tail_length} chars...]\n\n{text[-tail_length:]}"
    omitted = len(text) - head_length - tail_length
    return f"{text[:head_length]}\n\n[...truncated {omitted} chars...]\n\n{text[-tail_length:]}"


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
        send_to_session_fn: Optional[Callable[[str, str], bool]] = None,
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
        self._send_to_session = send_to_session_fn
        if label_manager is None:
            from .label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def _worktree_reuse_options(
        self,
        *,
        allow_remote_branch_delete: bool = True,
        force_fresh: bool = False,
    ) -> WorktreeReuseOptions:
        options = WorktreeReuseOptions(
            reuse_push_preflight=self.config.reuse_push_preflight,
            worktree_branch_on_recreate=self.config.worktree_branch_on_recreate,
            allow_no_verify_dry_run_preflight=self.config.allow_no_verify_dry_run_preflight,
            allow_remote_branch_delete=allow_remote_branch_delete,
        )
        if force_fresh:
            options.disable_reuse = True
            options.worktree_branch_on_recreate = "create_new_branch"
        return options

    @staticmethod
    def _extra_provider_args_from_labels(labels: Sequence[str]) -> dict[str, str] | None:
        """Build per-issue provider arg overrides from issue labels.

        Currently supports:
        - ``verbose`` label → ``{"verbose": "true"}``
        """
        args: dict[str, str] = {}
        if "verbose" in labels:
            args["verbose"] = "true"
        return args or None

    @staticmethod
    def _session_identity_launch_metadata(
        agent_config: "AgentConfig",
        *,
        extra_provider_args: dict[str, str] | None,
    ) -> dict[str, object]:
        provider_args = dict(agent_config.provider_args)
        permission_mode = str(provider_args.get("permission_mode") or agent_config.permission_mode or "")
        return {
            "provider": str(agent_config.provider or ""),
            "model": str(agent_config.model or ""),
            "permission_mode": permission_mode,
            "timeout_minutes": int(agent_config.timeout_minutes),
            "extra_provider_args": dict(extra_provider_args or {}),
        }

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

    def _interrupted_retry_guard_label(self, mode: str) -> str:
        retry_cfg = self.config.retry.interrupted_sessions
        if mode == "coding":
            return retry_cfg.coding_guard_label
        return retry_cfg.review_guard_label

    def _clear_interrupted_retry_guard_label(self, *, issue_number: int, mode: str, context: str) -> None:
        """Best-effort cleanup of interrupted retry guard at launch boundary."""
        guard_label = self._interrupted_retry_guard_label(mode)
        self._apply_actions([
            RemoveLabelAction(
                issue_number=issue_number,
                label=guard_label,
                reason=f"{mode} session relaunched - clearing interrupted retry guard",
            ),
        ], context=context)

    def _clear_reset_retry_pending_label(self, *, issue_number: int, context: str) -> None:
        """Best-effort cleanup of reset+retry pending guard at launch boundary."""
        pending_label = getattr(self._lm, "reset_retry_pending", None)
        if not isinstance(pending_label, str) or not pending_label:
            resolver = getattr(self._lm, "resolve", None)
            if callable(resolver):
                resolved = resolver("reset-retry-pending")
                pending_label = resolved if isinstance(resolved, str) and resolved else "reset-retry-pending"
            else:
                pending_label = "reset-retry-pending"
        actions: list[Action] = [
            RemoveLabelAction(
                issue_number=issue_number,
                label=pending_label,
                reason="session launched - clearing reset+retry pending guard",
            ),
        ]
        self._apply_actions(actions, context=context)

    def _clear_reset_retry_scratch_pending_label(self, *, issue_number: int, context: str) -> None:
        """Best-effort cleanup of reset+retry-from-scratch pending guard."""
        pending_label = getattr(self._lm, "reset_retry_scratch_pending", None)
        if not isinstance(pending_label, str) or not pending_label:
            resolver = getattr(self._lm, "resolve", None)
            if callable(resolver):
                resolved = resolver("reset-retry-scratch-pending")
                pending_label = (
                    resolved
                    if isinstance(resolved, str) and resolved
                    else "reset-retry-scratch-pending"
                )
            else:
                pending_label = "reset-retry-scratch-pending"
        actions: list[Action] = [
            RemoveLabelAction(
                issue_number=issue_number,
                label=pending_label,
                reason="session launched - clearing reset+retry-from-scratch pending guard",
            ),
        ]
        self._apply_actions(actions, context=context)

    def _build_session_env(
        self,
        *,
        completion_path: str,
        session_id: str,
        agent_label: str,
        issue_number: int,
        run_dir: Path,
        worktree_path: Path,
    ) -> str:
        """Build the common env-export string for all session types.

        Includes the orchestrator venv on PATH so ``coding-done``/``reviewer-done``
        is always reachable — even when the target repo is a foreign
        (non-orchestrator) repository with no ``.venv``.

        Also exports orchestrator ``src`` on ``PYTHONPATH`` so subprocess
        commands launched from arbitrary worktree directories can import
        ``issue_orchestrator`` without depending on editable installs.

        NOTE: The selected orchestrator config name is exported so ``coding-done``/``reviewer-done``
        resolves validation from the same config file used by the launcher.
        """
        orch_bin = Path(sys.executable).parent
        orch_src = Path(__file__).resolve().parents[2]
        runtime_tool_assignments = " ".join(build_runtime_tool_env_assignments(worktree_path))
        config_exports = ""
        if self.config.config_path is not None:
            config_name = self.config.config_path.name
            config_path = str(self.config.config_path.resolve())
            config_exports = (
                f" {ENV_PREFIX}CONFIG_NAME='{config_name}'"
                f" {ENV_PREFIX}CONFIG_PATH='{config_path}'"
            )
        return (
            f"export {ENV_PREFIX}COMPLETION_PATH='{completion_path}'"
            f" {ENV_PREFIX}SESSION_ID='{session_id}'"
            f" {ENV_PREFIX}AGENT_LABEL='{agent_label}'"
            f" {ENV_PREFIX}ISSUE_NUMBER='{issue_number}'"
            f"{config_exports}"
            f" {ENV_PREFIX}API_PORT='{self.config.control_api_port}'"
            f" {ENV_PREFIX}VALIDATION_OUTPUT_DIR='{run_dir}'"
            f" {ENV_PREFIX}RUN_DIR='{run_dir}'"
            f" {ENV_PREFIX}WORKTREE='{worktree_path}'"
            f" {runtime_tool_assignments}"
            f' PYTHONPATH="{orch_src}:${{PYTHONPATH:-}}"'
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
            return LaunchResult(None, False, "Terminal session already running", keep_queued=True)

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
            self.events.publish(make_trace_event(
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
            self.events.publish(make_trace_event(
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
            self.events.publish(make_trace_event(
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
        lease_seconds = self.config.claims.lease_seconds
        acquired_at = datetime.now()
        logger.info(issue_log(issue.number, "Claim acquired: lease_id=%s"), claim_result.lease_id)
        self.events.publish(make_trace_event(
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
        from_scratch_pending = self._lm.reset_retry_scratch_pending in issue.labels
        scratch_branch_name: str | None = None
        if from_scratch_pending:
            scratch_branch_name = f"{issue.number}-scratch-{int(time.time())}"
            logger.info(
                issue_log(
                    issue.number,
                    "Reset+retry from scratch requested; forcing fresh branch from base: %s",
                ),
                scratch_branch_name,
            )
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
            branch_name=scratch_branch_name,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(force_fresh=from_scratch_pending),
            phase_name=phase_name,
        )

        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            write_worktree_diagnostic(ctx.error)
            needs_human_label = self._lm.needs_human
            self._apply_actions([
                AddLabelAction(
                    issue_number=issue.number,
                    label=needs_human_label,
                    reason="worktree preparation failed",
                ),
                AddCommentAction(
                    number=issue.number,
                    comment=build_worktree_error_comment(ctx.error),
                    reason="worktree preparation failed",
                ),
            ], context="worktree_prepare_issue")
            self.events.publish(make_trace_event(
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
        extra_args = self._extra_provider_args_from_labels(issue.labels)

        # Write session metadata
        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.CODE.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": issue.agent_type,
            "reset_from_scratch": from_scratch_pending,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })
        if from_scratch_pending:
            ctx.update_manifest({
                "reset_from_scratch": True,
                "review_cache_boundary": "scratch_reset",
                "review_cache_boundary_started_at": run.started_at,
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
            try:
                self._run_setup_commands(worktree_path)
            except Exception as e:
                log_transition("issue", issue.number, "LAUNCHING", "FAILED", "setup commands failed")
                logger.error(issue_log(issue.number, "FAILED: setup commands failed: %s"), e)
                self.events.publish(make_trace_event(
                    EventName.SESSION_START_FAILED,
                    {
                        "issue_number": issue.number,
                        "session_name": session_name,
                        "reason": "setup_commands_failed",
                        "error": str(e),
                    },
                ))
                try:
                    self._worktree_manager.remove(worktree_path)
                    logger.info(issue_log(issue.number, "Cleaned up worktree after setup failure: %s"), worktree_path)
                except Exception as cleanup_error:
                    logger.warning(
                        issue_log(issue.number, "Failed to remove worktree after setup failure: %s"),
                        cleanup_error,
                    )
                self._release_claim_if_held(issue.number, claim)
                return LaunchResult(None, False, f"Setup commands failed: {e}")

        # New coding attempt starts now; clear interrupted retry guard.
        self._clear_interrupted_retry_guard_label(
            issue_number=issue.number,
            mode="coding",
            context="launch_clear_interrupted_guard_coding",
        )
        self._clear_reset_retry_pending_label(
            issue_number=issue.number,
            context="launch_clear_reset_retry_pending_coding",
        )
        self._clear_reset_retry_scratch_pending_label(
            issue_number=issue.number,
            context="launch_clear_reset_retry_scratch_pending_coding",
        )

        # Add in-progress label
        step_start = time.time()
        in_progress_label = self._lm.in_progress
        label_ok = self._apply_actions([
            AddLabelAction(
                issue_number=issue.number,
                label=in_progress_label,
                reason="session launched",
                issue_key=issue.key.stable_id(),
            ),
        ], context="launch_in_progress_label")
        if not label_ok:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "in-progress label failed")
            logger.error(issue_log(issue.number, "FAILED: could not add in-progress label"))
            self.events.publish(make_trace_event(
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
        existing_work = detect_existing_work(
            worktree_path,
            self._working_copy,
            seed_ref=self.config.worktree_seed_ref,
        )
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
        rendered_prompt = agent_config.render_initial_prompt(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            existing_work=existing_work,
        )
        prompt_path = self._persist_session_prompt(run.run_dir, rendered_prompt)
        base_command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            existing_work=existing_work,
            task_kind=TaskKind.CODE.value,
            extra_provider_args=extra_args,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(issue.agent_type, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=issue.agent_type,
            issue_number=issue.number,
            run_dir=run.run_dir,
            worktree_path=worktree_path,
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
                    issue_key=issue.key.stable_id(),
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
            original_prompt=rendered_prompt,
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
        session_started_payload: SessionStartedEventPayload = {
            "issue_number": issue.number,
            "session_id": session_name,
            "agent": issue.agent_type,
            "task": "code",
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
            "reset_from_scratch": from_scratch_pending,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
        }
        if from_scratch_pending:
            session_started_payload["review_cache_boundary_started_at"] = run.started_at
        self.events.publish(make_session_started_event(session_started_payload))

        # State machine transitions
        self._trigger_issue_session_state_transitions(issue, session_name, agent_config.timeout_minutes)

        return LaunchResult(session, True)

    def launch_validation_retry_session(
        self,
        retry: PendingValidationRetry,
        active_sessions: list[Session],
    ) -> LaunchResult:
        """Launch a coding session that continues after validation failure."""
        issue = self._resolve_validation_retry_issue(retry)
        if issue is None or issue.agent_type is None:
            return LaunchResult(
                None,
                False,
                f"No agent config available for validation retry #{retry.issue_number}",
            )

        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            return LaunchResult(None, False, f"No agent config for {issue.agent_type}")

        session_name = f"issue-{issue.number}"
        if result := self._check_launch_preconditions(issue, active_sessions, session_name):
            return result
        if result := self._check_provider_circuit(agent_config.provider, issue.number):
            return result

        retry_count = max(1, retry.retry_count)
        issue_key = issue.key
        session_key = SessionKey(issue=issue_key, task=TaskKind.CODE)
        logger.info(
            "[launch] Validation retry identity: issue=%s issue_key=%s agent=%s "
            "task=%s session=%s retry_count=%s",
            issue.number,
            issue_key,
            issue.agent_type,
            TaskKind.CODE.value,
            session_name,
            retry_count,
            extra=log_context(issue_key=issue_key.stable_id(), session_id=session_name),
        )
        log_transition(
            "issue",
            issue.number,
            "VALIDATION_RETRY_QUEUED",
            "LAUNCHING",
            f"retry_count={retry_count}",
        )

        claim = self._acquire_issue_claim(issue)
        if not claim.success:
            return claim.as_launch_failure()

        phase_name = f"coding-{retry_count + 1}"
        ctx = WorktreeContext.create(
            worktree_manager=self._worktree_manager,
            config=self.config,
            events=self.events,
            session_output=self._session_output,
            issue_number=issue.number,
            issue_title=issue.title,
            session_name=session_name,
            agent_label=issue.agent_type,
            branch_name=retry.branch_name or None,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
            reuse_options=self._worktree_reuse_options(allow_remote_branch_delete=False),
            phase_name=phase_name,
        )
        if ctx.error:
            log_transition("issue", issue.number, "LAUNCHING", "BLOCKED", "worktree preparation failed")
            logger.error(issue_log(issue.number, "BLOCKED: worktree preparation failed: %s"), ctx.error)
            write_worktree_diagnostic(ctx.error)
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, f"Worktree preparation failed: {ctx.error}")

        worktree_path = ctx.worktree_path
        branch_name = ctx.branch_name
        run = ctx.run
        extra_args = self._extra_provider_args_from_labels(issue.labels)
        retry_prompt = self._render_validation_retry_prompt(
            retry=retry,
            issue=issue,
            agent_config=agent_config,
            retry_count=retry_count,
        )

        ctx.write_worktree_note()
        ctx.write_session_identity({
            "task": TaskKind.CODE.value,
            "issue_key": issue_key.stable_id(),
            "session_key": session_key.stable_id(),
            "agent": issue.agent_type,
            "validation_retry": True,
            "validation_retry_count": retry_count,
            "validation_error_file": retry.validation_error_file,
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=extra_args,
            ),
        })
        ctx.update_manifest({
            "validation_retry": True,
            "validation_retry_count": retry_count,
            "validation_error": retry.validation_error,
            "validation_error_file": retry.validation_error_file,
        })

        if setup_failure := self._run_validation_retry_setup(issue, worktree_path, claim):
            return setup_failure

        self._clear_interrupted_retry_guard_label(
            issue_number=issue.number,
            mode="coding",
            context="launch_clear_interrupted_guard_validation_retry",
        )
        self._clear_reset_retry_pending_label(
            issue_number=issue.number,
            context="launch_clear_reset_retry_pending_validation_retry",
        )
        self._clear_reset_retry_scratch_pending_label(
            issue_number=issue.number,
            context="launch_clear_reset_retry_scratch_pending_validation_retry",
        )

        label_ok = self._apply_actions([
            AddLabelAction(
                issue_number=issue.number,
                label=self._lm.in_progress,
                reason="validation retry launched",
                issue_key=issue.key.stable_id(),
            ),
        ], context="launch_validation_retry_in_progress_label")
        if not label_ok:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "in-progress label failed")
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, "Failed to add in-progress label")

        prompt_path = self._persist_session_prompt(run.run_dir, retry_prompt)
        self._session_output.write_retry_prompt(run.run_dir, retry_prompt)
        base_command = agent_config.get_command_for_prompt(
            retry_prompt,
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            task_kind=TaskKind.CODE.value,
            extra_provider_args=extra_args,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(issue.agent_type, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=issue.agent_type,
            issue_number=issue.number,
            run_dir=run.run_dir,
            worktree_path=worktree_path,
        )
        command = f"{env_exports} && {base_command}"
        logger.info(
            "[launch] Validation retry command: issue=%s session=%s worktree=%s "
            "completion=%s command=%s",
            issue.number,
            session_name,
            worktree_path,
            completion_path,
            command,
        )

        session_created = self._create_session(session_name, command, worktree_path, issue.title)
        if not session_created:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
            self._apply_actions([
                RemoveLabelAction(
                    issue_number=issue.number,
                    label=self._lm.in_progress,
                    reason="validation retry session creation failed",
                    issue_key=issue.key.stable_id(),
                ),
            ], context="launch_validation_retry_session_creation_failed")
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, "Failed to create terminal session")

        session = Session(
            key=session_key,
            issue=issue,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            completion_path=completion_path,
            agent_label=issue.agent_type,
            validation_retry_count=retry_count,
            original_prompt=retry.original_prompt,
            lease_id=claim.lease_id,
            lease_acquired_at=claim.lease_acquired_at,
            lease_expires_at=claim.lease_expires_at,
        )
        log_transition(
            "issue",
            issue.number,
            "LAUNCHING",
            "ACTIVE",
            f"validation retry launched retry_count={retry_count}",
        )

        full_completion_path = (worktree_path / completion_path).resolve()
        self.events.publish(make_session_started_event({
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
            "session_prompt_path": prompt_path,
            "retry_count": retry_count,
        }))
        self._trigger_issue_session_state_transitions(issue, session_name, agent_config.timeout_minutes)
        return LaunchResult(session, True)

    def _run_validation_retry_setup(
        self,
        issue: Issue,
        worktree_path: Path,
        claim: ClaimAcquisitionResult,
    ) -> LaunchResult | None:
        """Run setup commands before a validation retry without deleting work."""
        if not self.config.setup_worktree:
            return None
        try:
            self._run_setup_commands(worktree_path)
        except Exception as e:
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "setup commands failed")
            logger.error(issue_log(issue.number, "FAILED: setup commands failed: %s"), e)
            self._release_claim_if_held(issue.number, claim)
            return LaunchResult(None, False, f"Setup commands failed: {e}")
        return None

    def _resolve_validation_retry_issue(self, retry: PendingValidationRetry) -> Issue | None:
        """Resolve a pending validation retry into an issue snapshot with an agent."""
        fresh_issue = self._refresh_issue(retry.issue_number) if self._refresh_issue else None
        agent_label = retry.agent_label or (fresh_issue.agent_type if fresh_issue else None)
        if not agent_label:
            return None
        labels = list(fresh_issue.labels) if fresh_issue else []
        if agent_label not in labels:
            labels.append(agent_label)
        return Issue(
            number=retry.issue_number,
            title=(fresh_issue.title if fresh_issue else retry.issue_title),
            labels=labels,
            state=(fresh_issue.state if fresh_issue else "open"),
            repo=self.config.repo or "",
            milestone=(fresh_issue.milestone if fresh_issue else None),
            body=(fresh_issue.body if fresh_issue else None),
            milestone_number=(fresh_issue.milestone_number if fresh_issue else None),
            milestone_due_on=(fresh_issue.milestone_due_on if fresh_issue else None),
        )

    def _render_validation_retry_prompt(
        self,
        *,
        retry: PendingValidationRetry,
        issue: Issue,
        agent_config: AgentConfig,
        retry_count: int,
    ) -> str:
        """Render the prompt used to send a validation failure back to a coder."""
        if retry.original_prompt and retry.original_prompt.lstrip().startswith("# Validation Retry"):
            return retry.original_prompt
        validation_cmd = retry.validation_cmd or (self.config.validation.cmd if self.config.validation else None) or ""
        original_task = retry.original_prompt or f"Work on issue #{issue.number}: {issue.title}"
        template = DEFAULT_RETRY_TEMPLATE
        template_path = agent_config.retry_prompt_template or self.config.retry.retry_prompt_template
        if template_path:
            full_template_path = self.config.repo_root / template_path
            if full_template_path.exists():
                try:
                    template = full_template_path.read_text()
                except OSError as exc:
                    logger.warning("Failed to load retry template from %s: %s", full_template_path, exc)
            else:
                logger.warning("Retry template not found at %s, using default", full_template_path)
        display_count = retry_count + 1
        display_max = self.config.retry.max_validation_retries + 1
        return template.format(
            original_task=original_task,
            validation_cmd=validation_cmd,
            error_file=retry.validation_error_file or "unknown",
            error_summary=_truncate_with_tail(retry.validation_error or "Unknown validation error"),
            retry_count=display_count,
            max_retries=display_max,
            retries_remaining=max(0, display_max - display_count),
        )

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
        validity = review_launch_validity(
            review=review,
            config=self.config,
            repository_host=self.repository_host,
            label_manager=self._lm,
        )
        if not validity.valid:
            log_transition(
                "review",
                review.pr_number,
                "QUEUED",
                "SKIP",
                f"stale pending review: {validity.reason}",
            )
            logger.info(
                "[launch] Dropping stale pending review: pr=%s issue=%s reason=%s issue_labels=%s pr_labels=%s",
                review.pr_number,
                review.issue_number,
                validity.reason,
                ",".join(validity.issue_labels) or "(missing)",
                ",".join(validity.pr_labels) or "(none)",
            )
            self.events.publish(
                make_trace_event(
                    EventName.REVIEW_SKIPPED,
                    {
                        "pr_number": review.pr_number,
                        "issue_number": review.issue_number,
                        "reason": f"stale_pending_review:{validity.reason}",
                    },
                )
            )
            return LaunchResult(None, False, f"Stale pending review: {validity.reason}")

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
            write_worktree_diagnostic(ctx.error)
            needs_human_label = self._lm.needs_human
            self._apply_actions([
                AddLabelAction(
                    issue_number=review.issue_number,
                    label=needs_human_label,
                    reason="worktree preparation failed",
                ),
                AddCommentAction(
                    number=review.issue_number,
                    comment=build_worktree_error_comment(ctx.error),
                    reason="worktree preparation failed",
                ),
            ], context="worktree_prepare_review")
            self.events.publish(make_trace_event(
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
            **self._session_identity_launch_metadata(
                agent_config,
                extra_provider_args=None,
            ),
        })
        # New review attempt starts now; clear interrupted retry guard.
        self._clear_interrupted_retry_guard_label(
            issue_number=review.issue_number,
            mode="review",
            context="launch_clear_interrupted_guard_review",
        )
        self._clear_reset_retry_pending_label(
            issue_number=review.issue_number,
            context="launch_clear_reset_retry_pending_review",
        )
        self._clear_reset_retry_scratch_pending_label(
            issue_number=review.issue_number,
            context="launch_clear_reset_retry_scratch_pending_review",
        )

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

        existing_work = build_review_existing_work(
            worktree_info=worktree_info,
            pr_number=review.pr_number,
            repository_host=self.repository_host,
            keep_current_label=self._lm.review_keep_approach,
        )

        # Build command
        rendered_prompt = agent_config.render_initial_prompt(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
        )
        prompt_path = self._persist_session_prompt(run.run_dir, rendered_prompt)
        base_command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
            existing_work=existing_work,
            task_kind=TaskKind.REVIEW.value,
        )
        base_command = self._wrap_provider_command(base_command, agent_config, run.run_dir)
        completion_path = get_completion_path(agent_label, run_dir=run.run_dir.name)
        self._session_output.update_manifest(
            run.run_dir,
            {
                "completion_path": completion_path,
                "session_prompt_path": prompt_path,
            },
        )
        env_exports = self._build_session_env(
            completion_path=completion_path,
            session_id=run.session_name,
            agent_label=agent_label,
            issue_number=review.issue_number,
            run_dir=run.run_dir,
            worktree_path=worktree_path,
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
        self.events.publish(make_run_scoped_event(EventName.REVIEW_STARTED, {
            "pr_number": review.pr_number,
            "issue_number": review.issue_number,
            "agent": agent_label,
            "task": "review",
            "session_name": session_name,
            "run_id": run.run_id,
            "run_dir": str(run.run_dir),
            "completion_path": completion_path,
            "completion_path_absolute": str(full_completion_path),
            "session_prompt_path": prompt_path,
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
        deps = ReworkLaunchDependencies(
            config=self.config,
            events=self.events,
            repository_host=self.repository_host,
            worktree_manager=self._worktree_manager,
            session_output=self._session_output,
            label_manager=self._lm,
            session_exists=self._session_exists,
            create_session=self._create_session,
            apply_actions=self._apply_actions,
            worktree_reuse_options=self._worktree_reuse_options,
            session_identity_launch_metadata=self._session_identity_launch_metadata,
            clear_interrupted_retry_guard_label=self._clear_interrupted_retry_guard_label,
            clear_reset_retry_pending_label=self._clear_reset_retry_pending_label,
            clear_reset_retry_scratch_pending_label=self._clear_reset_retry_scratch_pending_label,
            persist_session_prompt=self._persist_session_prompt,
            wrap_provider_command=self._wrap_provider_command,
            build_session_env=self._build_session_env,
            check_provider_circuit=self._check_provider_circuit,
        )
        return launch_rework_flow(rework, active_sessions, deps)

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
                env=build_runtime_tool_env(worktree_path),
            )
            if result.timed_out:
                logger.error("[launch] Setup command timed out: %s", cmd)
                raise RuntimeError(f"setup command timed out: {cmd}")
            if result.returncode != 0:
                stderr = result.stderr.strip() or "no stderr captured"
                logger.error("Setup command failed: %s\n%s", cmd, stderr)
                raise RuntimeError(
                    f"setup command failed: {cmd} (exit_code={result.returncode}): {stderr}"
                )
        setup_time = time.time() - step_start
        logger.info("[launch] Setup completed in %.1fs", setup_time)

    def _persist_session_prompt(self, run_dir: Path, prompt_text: str) -> str:
        """Persist rendered launch prompt into run-scoped artifacts."""
        prompt_path = self._session_output.write_session_prompt(run_dir, prompt_text)
        return str(prompt_path)

    def _is_interactive_provider(self, agent_config: "AgentConfig") -> bool:
        """Check if the agent uses an interactive provider (e.g. Claude Code TUI)."""
        if not agent_config.provider:
            return False
        from issue_orchestrator.agent_runner import get_provider, is_valid_provider
        if not is_valid_provider(agent_config.provider):
            return False
        return get_provider(agent_config.provider).interactive

    def _send_initial_prompt(self, session_name: str, prompt_path: Path, agent_config: "AgentConfig") -> None:
        """Send the initial prompt to an interactive session via PTY stdin.

        Instead of typing the full prompt text (which garbles in the TUI),
        we send a short file-reference instruction. The agent reads the file
        to get the full prompt content.
        """
        if not self._send_to_session:
            logger.warning("[launch] No send_to_session_fn configured; cannot deliver prompt to %s", session_name)
            return
        # Give the TUI time to initialize before sending the prompt.
        time.sleep(3)
        msg = f"Read and follow your instructions in {prompt_path}"
        sent = self._send_to_session(session_name, msg)
        logger.info("[launch] Sent initial prompt to interactive session %s: success=%s", session_name, sent)

    def _wrap_provider_command(self, base_command: str, agent_config: "AgentConfig", run_dir: Path) -> str:
        """Wrap provider command with retry/circuit reporting.

        Interactive providers are returned as-is — they manage their own
        lifecycle and don't use the provider_runner subprocess wrapper.
        """
        if self._is_interactive_provider(agent_config):
            return base_command
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
