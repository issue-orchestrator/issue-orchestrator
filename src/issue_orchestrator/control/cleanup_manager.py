"""CleanupManager - handles worktree and session cleanup.

This module extracts cleanup logic from the orchestrator:
1. process_deferred_cleanups - Clean up after reviews complete
2. recover_orphaned_cleanups - Clean up orphaned worktrees on startup

Cleanup is deferred when:
- Triage workflow: wait for triage-reviewed label
- Code review workflow: wait for code-reviewed label
"""

import logging
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config, AgentConfig
    from ..models import PendingCleanup
    from ..ports import RepositoryHost

from .._worktree_impl import remove_worktree, extract_issue_number_from_branch

logger = logging.getLogger(__name__)


class CleanupManager:
    """Manages worktree and session cleanup.

    Dependencies:
    - config: Configuration with cleanup settings
    - repository_host: For fetching PRs with labels
    - kill_session_fn: Function to kill terminal sessions
    - session_exists_fn: Function to check if session exists
    - get_worktree_path_fn: Function to get worktree path for issue
    """

    def __init__(
        self,
        config: "Config",
        repository_host: "RepositoryHost",
        kill_session_fn: Callable[[str], None],
        session_exists_fn: Callable[[str], bool],
        get_worktree_path_fn: Callable[[int, "AgentConfig"], Path],
        get_session_name_fn: Callable[[int, str], str],
    ):
        self.config = config
        self.repository_host = repository_host
        self._kill_session = kill_session_fn
        self._session_exists = session_exists_fn
        self._get_worktree_path = get_worktree_path_fn
        self._get_session_name = get_session_name_fn

    def process_deferred_cleanups(
        self,
        pending_cleanups: list["PendingCleanup"],
    ) -> list["PendingCleanup"]:
        """Process deferred cleanups for sessions awaiting review completion.

        Checks pending cleanups and performs cleanup when:
        - Triage workflow: PR has triage-reviewed label
        - Code review workflow: PR has code-reviewed label

        Args:
            pending_cleanups: List of pending cleanups to process

        Returns:
            Updated list with processed cleanups removed
        """
        if not pending_cleanups:
            return pending_cleanups

        # Determine which label indicates review is complete
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
        else:
            logger.warning("[CLEANUP] Found deferred cleanups but no review workflow configured")
            return pending_cleanups

        if not cleanup_label:
            logger.warning("[CLEANUP] No cleanup label configured")
            return pending_cleanups

        # Get all PRs with the cleanup label
        try:
            reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
            reviewed_pr_numbers = {pr.number for pr in reviewed_prs}
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")
            return pending_cleanups

        # Process each pending cleanup
        cleanups_to_remove = []
        for pending in pending_cleanups:
            if pending.pr_number in reviewed_pr_numbers:
                logger.info(f"[CLEANUP] PR #{pending.pr_number} has '{cleanup_label}' label - cleaning up")

                # Close terminal session if configured
                close_tabs = (
                    self.config.cleanup.with_triage.close_ai_session_tabs
                    if self.config.triage_review_agent
                    else self.config.cleanup.without_triage.close_ai_session_tabs
                )
                if close_tabs:
                    try:
                        self._kill_session(pending.terminal_session_name)
                        logger.info(f"[CLEANUP] Closed terminal session for #{pending.issue_number}")
                    except Exception as e:
                        logger.warning(f"[CLEANUP] Failed to close session for #{pending.issue_number}: {e}")

                # Remove worktree if configured
                remove_wt = (
                    self.config.cleanup.with_triage.remove_worktrees
                    if self.config.triage_review_agent
                    else self.config.cleanup.without_triage.remove_worktrees
                )
                if remove_wt:
                    try:
                        remove_worktree(pending.worktree_path)
                        logger.info(f"[CLEANUP] Removed worktree for #{pending.issue_number}")
                    except Exception as e:
                        logger.warning(f"[CLEANUP] Failed to remove worktree for #{pending.issue_number}: {e}")

                cleanups_to_remove.append(pending)

        # Remove processed cleanups
        remaining = [c for c in pending_cleanups if c not in cleanups_to_remove]

        if cleanups_to_remove:
            logger.info(f"[CLEANUP] Processed {len(cleanups_to_remove)} deferred cleanups")

        return remaining

    def recover_orphaned_cleanups(
        self,
        set_startup_message: Callable[[str], None] | None = None,
    ) -> int:
        """Recover and process orphaned cleanups from before restart.

        Called during startup to clean up worktrees for PRs that were reviewed
        (have triage-reviewed or code-reviewed label) but weren't cleaned up before
        the orchestrator stopped.

        Args:
            set_startup_message: Optional callback to set startup status message

        Returns:
            Number of orphaned worktrees cleaned up
        """
        # Determine which label indicates cleanup is due
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
            close_tabs = self.config.cleanup.with_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_triage.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_triage.remove_worktrees
        else:
            return 0

        if not cleanup_label:
            return 0

        if set_startup_message:
            set_startup_message("Checking for orphaned cleanups...")
        print(f"\nChecking for orphaned cleanups (PRs with '{cleanup_label}' label)...")

        try:
            reviewed_prs = self.repository_host.get_prs_with_label(cleanup_label)
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch reviewed PRs: {e}")
            return 0

        if not reviewed_prs:
            print("  No reviewed PRs found")
            return 0

        cleaned_count = 0
        for pr in reviewed_prs:
            # Extract issue number from branch name (e.g., "328-description" -> 328)
            branch = pr.branch
            issue_number = extract_issue_number_from_branch(branch)
            if issue_number is None:
                continue
            session_name = self._get_session_name(issue_number, "issue")

            # Check if session is still running (skip if so)
            if self._session_exists(session_name):
                logger.debug(f"[CLEANUP] Session {session_name} still running - skipping")
                continue

            # Check each agent config for matching worktree
            for agent_label, agent_config in self.config.agents.items():
                worktree_path = self._get_worktree_path(issue_number, agent_config)

                if worktree_path.exists():
                    logger.info(f"[CLEANUP] Found orphaned worktree for #{issue_number} at {worktree_path}")
                    print(f"  #{issue_number}: Cleaning up orphaned worktree")

                    # Close terminal if configured (may already be closed)
                    if close_tabs:
                        try:
                            self._kill_session(session_name)
                        except Exception:
                            pass  # Session probably already gone

                    # Remove worktree if configured
                    if remove_wt:
                        try:
                            remove_worktree(worktree_path)
                            logger.info(f"[CLEANUP] Removed orphaned worktree for #{issue_number}")
                        except Exception as e:
                            logger.warning(f"[CLEANUP] Failed to remove worktree for #{issue_number}: {e}")

                    cleaned_count += 1
                    break  # Found the worktree, no need to check other agents

        if cleaned_count > 0:
            print(f"  Cleaned up {cleaned_count} orphaned worktree(s)")
        else:
            print("  No orphaned worktrees found")

        return cleaned_count
