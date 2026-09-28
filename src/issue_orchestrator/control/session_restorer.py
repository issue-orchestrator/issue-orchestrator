"""SessionRestorer - handles restoring session tracking after restart.

This module extracts session restoration logic from the orchestrator.
It handles:
1. Discovering running sessions from the terminal backend
2. Finding corresponding worktrees
3. Fetching issue details
4. Creating Session objects for tracking

Called during startup to restore tracking for sessions that survived a restart.
"""

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports.recorded_run_reader import RecordedRunReader
    from ..ports.tech_lead_authority import TechLeadAuthorityStore

from ..infra.repo_scope import require_repo
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable, ReworkTarget
from ..domain.issue_key import GitHubIssueKey
from ..domain.session_key import SessionKey
from ..domain.session_kind import SessionKind
from ..domain.models import Issue, RETROSPECTIVE_REVIEW_TERMINAL_PREFIX, Session
from ..domain.session_run import SessionRunAssets
from ..ports import RepositoryHost, WorkingCopy
from ..ports.session_runner import DiscoveredSession
from .tech_lead_scope_recovery import recover_tech_lead_launch_scope

logger = logging.getLogger(__name__)

_CANONICAL_SESSION_PREFIXES = (
    "issue-",
    "review-",
    RETROSPECTIVE_REVIEW_TERMINAL_PREFIX,
    "rework-",
    "tech-lead-",
)
_REVIEW_SESSION_RE = re.compile(r"^review-(\d+)$")
_REVIEW_TITLE_RE = re.compile(r"\bReview PR #(\d+)\b")


class SessionConfigurationIdentityError(RuntimeError):
    """A surviving session cannot be safely restored under this configuration."""


class SessionConfigurationModeMismatchError(SessionConfigurationIdentityError):
    """A surviving session belongs to another effective launch configuration."""


class SessionConfigurationIdentityVerificationError(SessionConfigurationIdentityError):
    """A surviving session's effective launch configuration cannot be verified."""


class RestoredSessionRoleUnrecordedError(RuntimeError):
    """A surviving session's run has no durably recorded role to restore it as.

    Its kind and agent label are what every later decision about it rests on;
    restoring it under a guessed role is how a tech-lead run came back as the
    focus issue's coder (#7273, #7347). It is left untracked instead.
    """


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
        *,
        run_ledger: "RecordedRunReader",
        tech_lead_authority: "TechLeadAuthorityStore | None" = None,
    ):
        self.config = config
        self.repository_host = repository_host
        self.working_copy = working_copy
        # The durable run ledger is where a surviving session's KIND and agent
        # role come back from (#7347): both were recorded at allocation, before
        # the terminal spawned, outside the agent-writable run directory.
        self._run_ledger = run_ledger
        # Durable cohort ledger, read when rebuilding a restored health
        # review's owned scope (#6994 round 1 F3). Optional so unrelated tests
        # need not wire it; without it a restored storm review still recovers
        # its GLOBAL flavor (the barrier that matters) with an empty cohort.
        self.tech_lead_authority = tech_lead_authority

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
                    logger.info(
                        "Restored tracking for session %s (issue #%d)",
                        session.terminal_id,
                        issue_number,
                    )
                    print(f"  Restored: {session.terminal_id} (#{issue_number})")

            except SessionConfigurationIdentityError:
                raise
            except Exception as e:
                logger.exception(
                    "Failed to restore session for issue #%d: %s", issue_number, e
                )
                print(f"  Warning: Failed to restore session for #{issue_number}: {e}")

        return restored

    def predates_run_roles(self, session_info: DiscoveredSession) -> bool:
        """Whether this registry entry's run was allocated before roles were recorded.

        Such a run has a ledger row whose role is NULL (#7189 added the role
        columns). Agents are PTY children that do not survive an engine stop,
        so an entry like this at startup is a stale registry row, not a live
        session: the caller treats it as dead - no restore, no quarantine,
        its claim requeued by the dead-run sweep. Anything else, including an
        entry whose run cannot be read at all, is left to normal restoration.
        """
        run_dir = session_info.get("run_dir")
        if type(run_dir) is not str or not run_dir:
            return False
        try:
            run_assets = self._required_run_assets(
                session_info, self.canonical_terminal_id(session_info)
            )
            return self._run_ledger.recorded_run(run_assets).agent_label is None
        except (SessionConfigurationIdentityVerificationError, IssueRunEvidenceUnavailable):
            return False  # unreadable: the normal restore path decides (and reports) it

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
        run_dir: Path,
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
            run_dir=str(run_dir),
        )
        return self.restore_sessions([discovered], already_tracked)

    def _assert_restored_session_mode(
        self,
        run_assets: SessionRunAssets,
        session_name: str,
    ) -> None:
        """Reject a relaunch that would reinterpret a live session under another mode."""
        identity_path = run_assets.run_dir / "session-identity.json"
        identity: dict[str, object] = {}
        if identity_path.is_file():
            try:
                loaded = json.loads(identity_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise SessionConfigurationIdentityVerificationError(
                    f"Cannot verify configuration identity for live session {session_name}: "
                    f"{identity_path} is unreadable"
                ) from exc
            if not isinstance(loaded, dict):
                raise SessionConfigurationIdentityVerificationError(
                    f"Cannot verify configuration identity for live session {session_name}: "
                    f"{identity_path} must contain a JSON object"
                )
            identity = loaded

        recorded_values = tuple(
            identity.get(key)
            for key in ("configuration_mode", "config_name", "config_fingerprint")
        )
        if not all(isinstance(value, str) for value in recorded_values):
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity for live session {session_name}: "
                f"{identity_path} lacks mode, config name, or effective fingerprint"
            )
        recorded_identity = cast(tuple[str, str, str], recorded_values)
        # The session is bound to the settings a running engine does NOT apply
        # live (``session_binding_fingerprint``, hashed from the operator's
        # input, so schema growth and default changes never move it). A
        # session launched before that stamp existed recorded only the old
        # whole-dataclass hash, which no later code can recompute: for it,
        # only mode and config name are verifiable (a one-way legacy read).
        legacy = "session_binding_fingerprint" not in identity
        binding = identity.get("session_binding_fingerprint")
        if not legacy and not isinstance(binding, str):
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity for live session {session_name}: "
                f"{identity_path} has a malformed session binding fingerprint"
            )
        if legacy:
            logger.warning(
                "Live session %s predates session binding fingerprints; verifying "
                "its configuration mode and name only", session_name,
            )
        bound_mismatch = not legacy and binding != self.config.session_binding_fingerprint
        if recorded_identity[:2] != (self.config.configuration_mode, self.config.config_name) or bound_mismatch:
            recorded_mode, recorded_config, recorded_fingerprint = recorded_identity
            raise SessionConfigurationModeMismatchError(
                "Cannot start Repository Engine with configuration "
                f"{self.config.configuration_mode!r}/{self.config.config_name!r} "
                f"({self.config.config_fingerprint[:12]}): live session {session_name!r} "
                f"was launched with {recorded_mode!r}/{recorded_config!r} "
                f"({recorded_fingerprint[:12]}). Drain or terminate live sessions before "
                "switching configuration or editing a setting that requires a restart."
            )

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
        session_name = self.canonical_terminal_id(session_info)

        # Skip if already tracking this session
        if any(s.terminal_id == session_name for s in already_tracked):
            logger.info("Session %s already tracked - skipping restore", session_name)
            return None

        run_assets = self._required_run_assets(session_info, session_name)
        self._assert_restored_session_mode(run_assets, session_name)
        kind, agent_label, rework_target = self._recorded_role(run_assets, session_name)

        # A rework's PR and cycle come from its own ledger row (#7347).
        restored_pr_number = rework_target.pr_number if rework_target is not None else None
        if kind is SessionKind.REVIEW:
            match = _REVIEW_SESSION_RE.match(session_name)
            restored_pr_number = int(match.group(1)) if match else issue_number

        worktree_path = run_assets.worktree_path
        branch_name = self._get_branch_name(worktree_path)

        # Fetch single issue details to get agent type
        # The repo guard runs BEFORE any Issue is minted: a repo-less Issue has no
        # durable identity, and constructing one first is how an empty scope used to
        # reach the run ledger (#7255).
        if not self.config.repo:
            logger.warning("No repo configured for session %s - skipping", session_name)
            return None

        issue_obj = self.repository_host.get_issue(issue_number)
        if not issue_obj:
            # Create minimal issue object for reviews or if issue not found
            issue_obj = Issue(
                number=issue_number,
                title=tab_name.replace("#", "").strip(),
                labels=[],
                repo=require_repo(self.config),
            )

        # The run's recorded agent role, never ``issue_obj.agent_type``: that is
        # whichever ``agent:`` label the tracker lists first, which for a
        # failure investigation is the focus issue's coder (#7273, #7347). The
        # configuration fingerprint was verified above, so a recorded label
        # that is not configured is corruption, not a settings edit.
        try:
            agent_config = self.config.agents[agent_label]
        except KeyError:
            raise RestoredSessionRoleUnrecordedError(
                f"live session {session_name} was launched as agent "
                f"{agent_label!r}, which this configuration does not define"
            ) from None

        # Create session with domain identity
        issue_key = GitHubIssueKey(repo=require_repo(self.config), external_id=str(issue_number))
        session_key = SessionKey(issue=issue_key, kind=kind)
        if kind is SessionKind.REWORK and self.tech_lead_authority is not None:
            from .scoped_rework import note_scoped_rework_started
            note_scoped_rework_started(self.tech_lead_authority, run_assets.identity)
        return Session(
            key=session_key,
            issue=issue_obj,
            agent_config=agent_config,
            terminal_id=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
            run_assets=run_assets,
            agent_label=agent_label,
            pr_number=restored_pr_number,
            rework_cycle=rework_target.cycle if rework_target is not None else None,
            # Rebuild the tech-lead launch grant from durable truth. Without it
            # a restored whole-board review stops acting as the exclusive
            # barrier it is, and the dashboard misreports it (#6994 F3).
            tech_lead_scope=recover_tech_lead_launch_scope(
                kind, self.config, issue_obj, self.tech_lead_authority, run_assets.identity
            ),
        )

    def _recorded_role(
        self, run_assets: SessionRunAssets, session_name: str
    ) -> tuple[SessionKind, str, ReworkTarget | None]:
        """The kind, agent role and rework target the ledger recorded for this run.

        Replaces reading the kind off the terminal-name prefix and the role off
        the issue's first agent label (#7347). Legacy ledger rows decode through
        ``SessionKind.from_ledger_stamps``; a row that never recorded a role
        (pre-role-recording allocations) is refused rather than guessed.
        """
        record = self._run_ledger.recorded_run(run_assets)
        if record.agent_label is None:
            raise RestoredSessionRoleUnrecordedError(
                f"live session {session_name} run {run_assets.run_id} has no "
                "durably recorded role; it will not be restored under a guess"
            )
        return record.session_key.kind, record.agent_label, record.rework_target

    def _required_run_assets(
        self,
        session_info: DiscoveredSession,
        session_name: str,
    ) -> SessionRunAssets:
        raw: object = session_info.get("run_dir")
        if type(raw) is not str or not raw:
            message = (
                f"Discovered active session {session_name} has no recorded run_dir"
            )
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            )
        run_dir = Path(raw)
        manifest_path = run_dir / "manifest.json"
        if not run_dir.exists() or not manifest_path.exists():
            message = f"Discovered active session {session_name} run assets are missing: {run_dir}"
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            return SessionRunAssets.from_manifest_payload(
                run_dir=run_dir,
                manifest=manifest,
            )
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            message = (
                f"Discovered active session {session_name} has invalid run assets at "
                f"{run_dir}: {exc}"
            )
            raise SessionConfigurationIdentityVerificationError(
                f"Cannot verify configuration identity: {message}"
            ) from exc

    def _get_branch_name(self, worktree_path: Path) -> str:
        """Get the current branch name for a worktree.

        Uses WorkingCopy to get branch name.
        """
        branch = self.working_copy.get_current_branch(worktree_path)
        if not branch:
            logger.warning("Failed to get branch name for %s", worktree_path)
            return "unknown"
        return branch

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

