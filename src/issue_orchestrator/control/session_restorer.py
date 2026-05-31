"""SessionRestorer - handles restoring session tracking after restart.

This module extracts session restoration logic from the orchestrator.
It handles:
1. Discovering running sessions from the terminal backend
2. Finding corresponding worktrees
3. Fetching issue details
4. Creating Session objects for tracking

Called during startup to restore tracking for sessions that survived a restart.
"""

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..infra.config import Config

from ..domain.issue_key import GitHubIssueKey
from ..domain.session_key import SessionKey, TaskKind
from ..domain.models import Issue, RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session
from ..ports import RepositoryHost, WorkingCopy
from ..ports.session_runner import DiscoveredSession

logger = logging.getLogger(__name__)

_CANONICAL_SESSION_PREFIXES = (
    "issue-",
    "review-",
    RETROSPECTIVE_REVIEW_TERMINAL_PREFIX,
    "rework-",
    "triage-",
)
_REVIEW_SESSION_RE = re.compile(r"^review-(\d+)$")
_REVIEW_TITLE_RE = re.compile(r"\bReview PR #(\d+)\b")


class SessionRestorer:
    """Handles restoring session tracking after orchestrator restart.

    Dependencies:
    - config: Configuration with agent settings
    - repository_host: For fetching issue details and cleanup
    """

    def __init__(
        self,
        config: "Config",
        repository_host: RepositoryHost,
        working_copy: WorkingCopy,
    ):
        self.config = config
        self.repository_host = repository_host
        self.working_copy = working_copy

    def restore_sessions(
        self,
        running: list[DiscoveredSession],
        already_tracked: list[Session],
    ) -> list[Session]:
        """Restore tracking for sessions that are still running after restart.

        Args:
            running: List of dicts from discover_running_sessions() with
                     {issue_number, tab_name, is_review}
            already_tracked: Sessions already being tracked (to avoid duplicates)

        Returns:
            List of newly restored Session objects
        """
        restored = []

        for session_info in running:
            issue_number = self._issue_number(session_info)

            try:
                session = self._restore_single_session(
                    session_info=session_info,
                    already_tracked=already_tracked + restored,
                )
                if session:
                    restored.append(session)
                    logger.info("Restored tracking for session %s (issue #%d)",
                               session.terminal_id, issue_number)
                    print(f"  Restored: {session.terminal_id} (#{issue_number})")

            except Exception as e:
                logger.exception("Failed to restore session for issue #%d: %s", issue_number, e)
                print(f"  Warning: Failed to restore session for #{issue_number}: {e}")

        return restored

    def canonical_terminal_id(self, session_info: DiscoveredSession) -> str:
        """Return the canonical terminal id for a discovered or known terminal."""
        session_name = str(session_info.get("session_name") or "")
        if session_name.startswith(_CANONICAL_SESSION_PREFIXES):
            return session_name

        issue_number = self._issue_number(session_info)
        tab_name = str(session_info.get("tab_name") or "")
        if session_info.get("is_review"):
            pr_number = self._review_pr_number(session_info)
            if pr_number is not None:
                return f"review-{pr_number}"
            logger.warning(
                "[ORPHAN] Could not derive review PR number from discovered session; "
                "falling back to issue number: issue=%s tab_name=%r session_name=%r",
                issue_number,
                tab_name,
                session_name,
            )
            return f"review-{issue_number}"

        if tab_name.startswith(_CANONICAL_SESSION_PREFIXES):
            return tab_name
        # Legacy records without session_name predate durable canonical ids.
        # Non-review records were overwhelmingly issue sessions, so issue-N is
        # the best recoverable identity if the tab title is also noncanonical.
        return f"issue-{issue_number}"

    def restore_known_terminal(
        self,
        *,
        issue_number: int,
        session_name: str,
        is_review: bool,
        already_tracked: list[Session],
        tab_name: str = "",
    ) -> list[Session]:
        """Restore tracking for a terminal whose canonical id is already known."""
        discovered = DiscoveredSession(
            issue_number=issue_number,
            tab_name=tab_name,
            is_review=is_review,
            session_name=session_name,
        )
        return self.restore_sessions([discovered], already_tracked)

    def _restore_single_session(
        self,
        session_info: DiscoveredSession,
        already_tracked: list[Session],
    ) -> Optional[Session]:
        """Restore a single session.

        Returns:
            Session object if restored, None if skipped
        """
        issue_number = self._issue_number(session_info)
        tab_name = str(session_info.get("tab_name") or "")
        is_review = session_info["is_review"]
        session_name = self.canonical_terminal_id(session_info)

        # Determine session type and session_name
        restored_pr_number: int | None = None
        if is_review and not session_name.startswith(RETROSPECTIVE_REVIEW_TERMINAL_PREFIX):
            match = _REVIEW_SESSION_RE.match(session_name)
            restored_pr_number = int(match.group(1)) if match else issue_number

        # Skip if already tracking this session
        if any(s.terminal_id == session_name for s in already_tracked):
            logger.info("Session %s already tracked - skipping restore", session_name)
            return None

        # Find the worktree
        worktree_path, branch_name = self._find_worktree(issue_number)

        if not worktree_path:
            logger.warning("Could not find worktree for session %s - cleaning up orphaned issue", session_name)
            self._cleanup_orphaned_issue(issue_number)
            return None

        # Fetch single issue details to get agent type
        issue_obj = self.repository_host.get_issue(issue_number)
        agent_config = None

        if issue_obj and issue_obj.agent_type:
            agent_config = self.config.agents.get(issue_obj.agent_type)

        if not issue_obj:
            # Create minimal issue object for reviews or if issue not found
            issue_obj = Issue(
                number=issue_number,
                title=tab_name.replace("#", "").strip(),
                labels=[],
            )

        if not agent_config:
            # Use first available agent config as fallback
            agent_config = next(iter(self.config.agents.values()), None)

        if not agent_config:
            logger.warning("No agent config available for session %s - skipping", session_name)
            return None

        if not self.config.repo:
            logger.warning("No repo configured for session %s - skipping", session_name)
            return None

        # Create session with domain identity
        issue_key = GitHubIssueKey(repo=self.config.repo, external_id=str(issue_number))
        if session_name.startswith(RETROSPECTIVE_REVIEW_TERMINAL_PREFIX):
            task_kind = TaskKind.RETROSPECTIVE_REVIEW
        else:
            task_kind = TaskKind.REVIEW if is_review else TaskKind.CODE
        session_key = SessionKey(issue=issue_key, task=task_kind)
        # Use the agent type from issue labels, or the first available agent as fallback
        agent_label_val = issue_obj.agent_type or next(iter(self.config.agents.keys()), "unknown")
        return Session(
            key=session_key,
            issue=issue_obj,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            agent_label=agent_label_val,
            pr_number=restored_pr_number,
        )

    def _find_worktree(self, issue_number: int) -> tuple[Optional[Path], str]:
        """Find the worktree for an issue.

        Returns:
            Tuple of (worktree_path, branch_name) or (None, "unknown") if not found
        """
        worktree_path = None
        branch_name = "unknown"

        # Check the repo_root for the worktree
        repo_root = self.config.repo_root
        candidate_path = repo_root.parent / f"{repo_root.name}-{issue_number}"
        if candidate_path.exists():
            worktree_path = candidate_path
            # Get branch name from worktree
            branch_name = self._get_branch_name(candidate_path)

        return worktree_path, branch_name

    def _get_branch_name(self, worktree_path: Path) -> str:
        """Get the current branch name for a worktree.

        Uses WorkingCopy to get branch name.
        """
        branch = self.working_copy.get_current_branch(worktree_path)
        if not branch:
            logger.warning("Failed to get branch name for %s", worktree_path)
            return "unknown"
        return branch

    def _cleanup_orphaned_issue(self, issue_number: int) -> None:
        """Clean up an orphaned issue by removing the in-progress label.

        Called when we find a session without a corresponding worktree.
        """
        logger.info(
            "Orphaned issue #%d detected - stale labels will be handled by planner cleanup",
            issue_number,
        )
        print(f"  Orphaned issue #{issue_number} (stale label cleanup deferred)")

    @staticmethod
    def _issue_number(session_info: DiscoveredSession) -> int:
        return int(session_info.get("issue_number") or 0)

    @staticmethod
    def _review_pr_number(session_info: DiscoveredSession) -> int | None:
        session_name = str(session_info.get("session_name") or "")
        match = _REVIEW_SESSION_RE.match(session_name)
        if match:
            return int(match.group(1))

        tab_name = str(session_info.get("tab_name") or "")
        match = _REVIEW_TITLE_RE.search(tab_name)
        if match:
            return int(match.group(1))
        return None
