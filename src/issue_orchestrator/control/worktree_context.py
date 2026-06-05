"""WorktreeContext - encapsulates worktree setup for agent sessions.

This dataclass consolidates the worktree-related data and operations
that are duplicated across launch_issue_session, launch_review_session,
and launch_rework_session.

Usage:
    ctx = WorktreeContext.create(
        worktree_manager=manager,
        config=config,
        events=events,
        issue_number=123,
        issue_title="Fix bug",
        session_name="issue-123",
        branch_name=None,  # Auto-generate for issues
        enforce_hooks=True,
        allow_remote_branch_delete=True,
    )
    if ctx.error:
        return LaunchResult(None, False, str(ctx.error))

    ctx.write_session_identity({"agent": "claude-code"})
"""

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..events import EventName
from ..infra.config import Config
from ..infra.logging_config import get_repo_log_path
from ..infra.repo_identity import get_repo_head_sha
from ..ports import EventSink,  make_trace_event
from ..domain.session_run import SessionRunAssets
from ..ports.session_output import SessionOutput
from ..ports.worktree_manager import WorktreeManager, WorktreeInfo, WorktreeReuseOptions
from .worktree import Worktree, WorktreePreparationError

logger = logging.getLogger(__name__)


def _escape_claude_project_path(path: Path) -> str:
    """Escape a worktree path into Claude Code's project directory name."""
    cleaned = str(path).lstrip("/")
    return "-" + cleaned.replace("/", "-")


@dataclass
class WorktreeContext:
    """Encapsulates worktree state and operations for a session.

    This dataclass is created via WorktreeContext.create() which handles
    all the worktree creation, preparation, and setup.

    Attributes:
        session_name: Terminal session identifier (globally unique, e.g., "issue-42")
        phase_name: Logical phase name for session output directories (e.g., "coding-1")
    """

    worktree_path: Path
    branch_name: str
    session_name: str
    phase_name: str
    issue_number: int
    worktree_info: WorktreeInfo
    run: SessionRunAssets
    claude_project_dir: Path
    error: Optional[WorktreePreparationError] = None

    # Internal references for write operations
    _config: Config = field(repr=False, default=None)  # type: ignore[assignment]
    _session_output: SessionOutput = field(repr=False, default=None)  # type: ignore[assignment]

    @classmethod
    def create(
        cls,
        *,
        worktree_manager: WorktreeManager,
        config: Config,
        events: EventSink,
        session_output: SessionOutput,
        issue_number: int,
        issue_title: str,
        session_name: str,
        agent_label: str,
        branch_name: Optional[str] = None,
        enforce_hooks: bool = False,
        pre_push_hook: Optional[Path] = None,
        reuse_options: Optional[WorktreeReuseOptions] = None,
        phase_name: Optional[str] = None,
    ) -> "WorktreeContext":
        """Create and prepare a worktree context for a session.

        Args:
            worktree_manager: For creating the worktree
            config: Configuration with repo settings
            events: For emitting trace events
            issue_number: Issue number
            issue_title: Issue title (for worktree naming)
            session_name: Terminal session identifier (globally unique, e.g., "issue-123")
            agent_label: Agent label for the session
            branch_name: Optional branch name (auto-generated if None)
            enforce_hooks: Whether to enforce git hooks
            pre_push_hook: Optional pre-push hook script
            reuse_options: Worktree reuse configuration
            phase_name: Logical phase name for session output (e.g., "coding-1").
                        Defaults to session_name if not provided.

        Returns:
            WorktreeContext with worktree ready for use, or with error set
        """
        # Default phase_name to session_name for backward compatibility
        if phase_name is None:
            phase_name = session_name
        repo_root = config.repo_root
        worktree_base = config.worktree_base

        # Per-session worktree base support
        if os.environ.get("ORCHESTRATOR_WORKTREE_PER_SESSION") == "1":
            base_root = Path(worktree_base) if worktree_base else repo_root.parent
            worktree_base = base_root / session_name
            logger.info(
                "[launch] Per-session worktree base: session=%s base=%s",
                session_name,
                worktree_base,
            )

        # Create worktree
        try:
            worktree_info = worktree_manager.create(
                repo_root=repo_root,
                issue_number=issue_number,
                issue_title=issue_title,
                worktree_base=worktree_base,
                base_branch=config.worktree_base_branch_override,
                seed_ref=config.worktree_seed_ref,
                enforce_hooks=enforce_hooks,
                reuse_options=reuse_options or WorktreeReuseOptions(),
                branch_name=branch_name,
                pre_push_hook=pre_push_hook,
            )
        except Exception as e:
            resolved_base = Path(worktree_base).resolve() if worktree_base else repo_root.parent
            worktree_path = resolved_base / f"{repo_root.name}-{issue_number}"
            preparation_error = WorktreePreparationError(
                path=worktree_path,
                issue_number=issue_number,
                message=f"Cannot create worktree {worktree_path.name}: {e}",
            )
            logger.error(
                "[issue-%d] Worktree create failed: path=%s error=%s",
                issue_number,
                worktree_path,
                e,
            )
            return cls(
                worktree_path=worktree_path,
                branch_name=branch_name or "",
                session_name=session_name,
                phase_name=phase_name,
                issue_number=issue_number,
                worktree_info=WorktreeInfo(
                    path=worktree_path,
                    branch_name=branch_name or "",
                    reuse_status="error",
                    reuse_reason="worktree_create_failed",
                    uncommitted_discarded=0,
                    commits_discarded=0,
                    rebase_failed=False,
                ),
                run=None,  # type: ignore[arg-type]
                claude_project_dir=Path(),
                error=preparation_error,
                _config=config,
                _session_output=session_output,
            )
        worktree_path = worktree_info.path
        actual_branch = worktree_info.branch_name

        # Emit event if work was discarded during worktree reset
        if worktree_info.uncommitted_discarded > 0 or worktree_info.commits_discarded > 0:
            events.publish(make_trace_event(
                EventName.WORKTREE_RESET,
                {
                    "issue_number": issue_number,
                    "branch_name": actual_branch,
                    "uncommitted_discarded": worktree_info.uncommitted_discarded,
                    "commits_discarded": worktree_info.commits_discarded,
                },
            ))

        # Prepare worktree - clean stale artifacts from previous sessions
        worktree = Worktree(
            worktree_path,
            issue_number,
            retain_runs=config.session_output_retention_runs,
            session_output=session_output,
        )
        try:
            worktree.prepare_for_session(phase_name)
        except WorktreePreparationError as e:
            # Return context with error - caller handles the error response
            return cls(
                worktree_path=worktree_path,
                branch_name=actual_branch,
                session_name=session_name,
                phase_name=phase_name,
                issue_number=issue_number,
                worktree_info=worktree_info,
                run=None,  # type: ignore[arg-type]
                claude_project_dir=Path(),
                error=e,
                _config=config,
                _session_output=session_output,
            )

        # Setup output directories and tracking
        claude_project_dir = Path.home() / ".claude" / "projects" / _escape_claude_project_path(worktree_path)
        run = session_output.start_run(
            worktree_path=worktree_path,
            session_name=phase_name,
            issue_number=issue_number,
            agent_label=agent_label,
            backend=config.terminal_adapter or "subprocess",
            claude_log_dir=str(claude_project_dir),
            orchestrator_log=str(get_repo_log_path(config.repo_root)),
            retention_tier=config.session_output_retention_tier,
            retention_days=config.session_output_retention_days,
            retention_pinned=False,
        )

        return cls(
            worktree_path=worktree_path,
            branch_name=actual_branch,
            session_name=session_name,
            phase_name=phase_name,
            issue_number=issue_number,
            worktree_info=worktree_info,
            run=run,
            claude_project_dir=claude_project_dir,
            _config=config,
            _session_output=session_output,
        )

    def write_worktree_note(self) -> None:
        """Write a JSON note about worktree reuse/creation for this session."""
        try:
            commit_sha = get_repo_head_sha(self.worktree_path)
            payload = {
                "issue_number": self.issue_number,
                "session_name": self.session_name,
                "worktree_path": str(self.worktree_path),
                "branch_name": self.branch_name,
                "commit_sha": commit_sha,
                "reuse_status": self.worktree_info.reuse_status,
                "reuse_reason": self.worktree_info.reuse_reason,
                "uncommitted_discarded": self.worktree_info.uncommitted_discarded,
                "commits_discarded": self.worktree_info.commits_discarded,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if self.run:
                self._session_output.write_worktree_note(self.run.run_dir, payload)
        except Exception as e:
            logger.warning("Failed to write worktree note: %s", e)

    def write_session_identity(self, extra: dict[str, object]) -> None:
        """Persist session identity details inside the worktree for later review.

        Args:
            extra: Additional session-specific fields (agent, task type, etc.)
        """
        try:
            commit_sha = get_repo_head_sha(self.worktree_path)
            payload = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "session_name": self.session_name,
                "issue_number": self.issue_number,
                "branch": self.branch_name,
                "commit_sha": commit_sha,
                "worktree": str(self.worktree_path),
                "claude_project_dir": str(self.claude_project_dir),
                "claude_args": os.environ.get("ORCHESTRATOR_CLAUDE_ARGS", ""),
                "claude_prompt_mode": os.environ.get("ORCHESTRATOR_CLAUDE_PROMPT_MODE", "arg"),
                **extra,
            }
            if self.run:
                self._session_output.write_session_identity(self.run.run_dir, payload)
                logger.info(
                    "[launch] Session identity file: session=%s run_dir=%s",
                    self.session_name,
                    self.run.run_dir,
                )
        except Exception as e:
            logger.warning("Failed to write session identity file: %s", e)

    def update_manifest(self, updates: dict[str, object]) -> None:
        """Update the session run manifest with additional data."""
        if self.run:
            self._session_output.update_manifest(self.run.run_dir, updates)
