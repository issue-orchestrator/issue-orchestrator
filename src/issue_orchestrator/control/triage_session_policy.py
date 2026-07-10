"""ADR-0031 owner boundary for triage session identity and completion effects.

Both triage variants (batch PR review and failure investigation) launch as
``issue-{N}`` sessions under the configured triage agent, so nothing about a
session's name distinguishes them. This module is the single owner for:

- **identity**: what makes a session a triage session (the config-declared
  triage agent), consolidating the checks previously duplicated in
  ``SessionLauncher`` and ``CompletionActionPlanner``;
- **flavor**: reading the launch-time :class:`TriageAssignment` that says
  which variant a session was given (manifest selection keys off it);
- **launch preparation**: per-flavor session inputs (PR manifest download,
  the agent-visible assignment copy) plus the orchestrator-owned
  :class:`TriageLaunchAuthority` record that completion later trusts
  (#6761 re-review F1);
- **completion effects**: shaping the requested actions a triage completion
  may execute and classifying the benign "clean audit, nothing to publish"
  outcome so it is treated as success rather than a publish failure.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import RequestedAction
from ..domain.triage_manifest import TriageManifest
from ..domain.triage_session import (
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageLaunchAuthority,
    TriageSessionFlavor,
)
from ..infra.triage_authority_store import TriageAuthorityStore
from .completion_pr_collision import NoCommitsBetweenError
from .triage_manifest_builder import TriageCandidatePolicy, TriageManifestBuilder

if TYPE_CHECKING:
    from ..infra.config import Config
    from ..ports import ManifestDownloader, RepositoryHost
    from ..ports.issue import Issue
    from .worktree_context import WorktreeContext

logger = logging.getLogger(__name__)


def is_triage_session(
    triage_review_agent: str | None, agent_type: str | None
) -> bool:
    """True when ``agent_type`` is the configured triage review agent."""
    return bool(triage_review_agent and agent_type == triage_review_agent)


def shape_requested_actions_for_triage(
    requested: tuple[RequestedAction, ...],
) -> tuple[RequestedAction, ...]:
    """Drop POST_COMMENT from a triage completion's requested actions.

    Triage prompts promise the orchestrator posts no comments; the generic
    "## Implementation" template would land on the tracking issue otherwise.
    PUSH_BRANCH/CREATE_PR stay: real prompt/doc improvements should publish.
    """
    return tuple(
        action for action in requested if action is not RequestedAction.POST_COMMENT
    )


def is_benign_triage_no_commits(
    action: RequestedAction, error: BaseException
) -> bool:
    """True when a triage CREATE_PR failed only because there is nothing to publish.

    A clean audit has nothing to publish; that is success, not publish-failure.
    """
    return action is RequestedAction.CREATE_PR and isinstance(
        error, NoCommitsBetweenError
    )


def read_triage_assignment(run_dir: Path) -> TriageAssignment | None:
    """Read the launch-time triage assignment from a session run directory.

    Returns None when the assignment file is absent (pre-upgrade sessions).
    Malformed content raises ValueError - callers decide the fail-safe.
    """
    path = run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
    if not path.exists():
        return None
    return TriageAssignment.read(path)


def prepare_triage_manifest(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    manifest_downloader: "ManifestDownloader",
    worktree_path: Path,
    run_dir: Path,
) -> TriageManifest | None:
    """Build and download the batch PR manifest for a triage session.

    Returns the populated manifest, or None when no PRs need triage.
    Eligibility comes from the shared candidate owner so the audited set
    matches the threshold set.
    """
    builder = TriageManifestBuilder(
        repository_host=repository_host,
        watch_label=config.triage_watch_label,
        candidate_policy=TriageCandidatePolicy.from_config(config),
    )

    # Data goes in session run directory
    data_dir = f".issue-orchestrator/sessions/{run_dir.name}/triage-data"
    manifest = builder.build(data_dir)

    if not manifest.prs:
        logger.info("[triage] No PRs need triage review")
        return None

    manifest = manifest_downloader.download(manifest, worktree_path)

    manifest_path = worktree_path / data_dir / "manifest.json"
    manifest.write(manifest_path)

    logger.info(
        "[triage] Prepared manifest with %d PRs: %s",
        len(manifest.prs),
        manifest_path,
    )
    return manifest


def prepare_triage_session_data(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    manifest_downloader: "ManifestDownloader",
    issue: "Issue",
    ctx: "WorktreeContext",
    triage_flavor: TriageSessionFlavor | None,
) -> None:
    """Prepare per-flavor triage session inputs (ADR-0031).

    BATCH_REVIEW keeps the existing PR-manifest prep; FAILURE_INVESTIGATION
    must NOT receive the global batch manifest (auditing unrelated PRs from
    a focused investigation was the #6768 B4 defect). Both flavors get a
    triage-assignment.json copy for the AGENT to read, and — the trusted
    half — an orchestrator-owned :class:`TriageLaunchAuthority` record
    persisted outside the agent-writable worktree, keyed by this run's
    identity, which completion reads as the only scope authority
    (#6761 re-review F1).
    """
    if not is_triage_session(config.triage_review_agent, issue.agent_type):
        return
    flavor = triage_flavor or TriageSessionFlavor.BATCH_REVIEW
    run_dir = ctx.run.run_dir
    triage_manifest = None
    if flavor is TriageSessionFlavor.BATCH_REVIEW:
        triage_manifest = prepare_triage_manifest(
            config=config,
            repository_host=repository_host,
            manifest_downloader=manifest_downloader,
            worktree_path=ctx.worktree_path,
            run_dir=run_dir,
        )
        if triage_manifest:
            # Store manifest path in session for completion handling
            ctx.update_manifest(
                {"triage_manifest": str(run_dir / "triage-data" / "manifest.json")}
            )
    focused = flavor is TriageSessionFlavor.FAILURE_INVESTIGATION
    assignment = TriageAssignment(
        flavor=flavor,
        focus_issue_number=issue.number if focused else None,
        focus_reason=issue.title if focused else "",
    )
    assignment_path = run_dir / "triage-data" / TRIAGE_ASSIGNMENT_FILENAME
    assignment.write(assignment_path)
    ctx.update_manifest({"triage_assignment": str(assignment_path)})
    TriageAuthorityStore.for_repo(config.repo_root).record(
        run_id=ctx.run.run_id,
        session_name=ctx.run.session_name,
        authority=TriageLaunchAuthority(
            flavor=flavor,
            anchor_issue_number=issue.number,
            focus_issue_number=issue.number if focused else None,
            manifest_pr_numbers=tuple(pr.number for pr in triage_manifest.prs)
            if triage_manifest
            else (),
        ),
    )
    logger.info("[triage] Wrote %s assignment: %s", flavor.value, assignment_path)
