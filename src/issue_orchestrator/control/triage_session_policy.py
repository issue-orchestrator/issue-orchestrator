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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.models import RequestedAction
from ..domain.triage_manifest import TriageManifest
from ..domain.board_snapshot import BOARD_SNAPSHOT_FILENAME, BoardSnapshot
from ..domain.triage_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TRIAGE_ASSIGNMENT_FILENAME,
    TriageAssignment,
    TriageLaunchAuthority,
    TriageLaunchScope,
    TriageSessionFlavor,
)
from .completion_pr_collision import NoCommitsBetweenError
from .triage_evidence import build_evidence_map, write_evidence_map
from .triage_manifest_builder import TriageCandidatePolicy, TriageManifestBuilder

if TYPE_CHECKING:
    from ..ports.board_snapshot_provider import BoardSnapshotProvider
    from ..infra.config import Config
    from ..ports import ManifestDownloader, RepositoryHost
    from ..ports.issue import Issue
    from ..ports.triage_authority import TriageAuthorityStore
    from .worktree_context import ScratchWorktreeIdentity, WorktreeContext

logger = logging.getLogger(__name__)


def is_triage_session(
    triage_review_agent: str | None, agent_type: str | None
) -> bool:
    """True when ``agent_type`` is the configured triage review agent."""
    return bool(triage_review_agent and agent_type == triage_review_agent)


def failure_investigation_scratch_identity(
    config: "Config",
    issue: "Issue",
    triage_scope: "TriageLaunchScope | None",
) -> "ScratchWorktreeIdentity | None":
    """The disposable scratch worktree identity for a failure investigation (#6823).

    A failure investigation launches as an ``issue-{focus}`` session under the
    focus issue's number, so without this it would run in the focus issue's OWN
    worktree on its branch — and the agent could commit into that branch,
    mutating the very evidence it was sent to read (a live run showed a focus
    branch advance by a junk agent commit). Gating on the producer-declared
    ``triage_scope.flavor`` (the reliable signal owned here, ADR-0031) it instead
    runs in a throwaway worktree on a fresh branch off the base branch, keyed to
    this run rather than the focus issue: the focus worktree/branch stay pure
    read-only evidence and an agent commit can only ever land on the disposable
    branch. The name and branch carry a random token so investigations of the
    same focus issue never collide, and the branch does NOT start with the focus
    issue number so ``extract_issue_number_from_branch`` never mistakes it for the
    focus branch.

    Returns ``None`` for every other flavor (batch/health reviews run on their
    own anchor worktrees) and for ordinary non-triage issues, leaving their
    worktree derivation unchanged.
    """
    from .worktree_context import ScratchWorktreeIdentity

    if (
        triage_scope is None
        or triage_scope.flavor is not TriageSessionFlavor.FAILURE_INVESTIGATION
    ):
        return None
    token = uuid.uuid4().hex[:12]
    return ScratchWorktreeIdentity(
        worktree_name=f"{config.repo_root.name}-triage-{issue.number}-{token}",
        branch_name=f"triage-investigation-{issue.number}-{token}",
    )


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


def _resolve_health_review_cohort(
    triage_scope: "TriageLaunchScope | None",
    *,
    triage_authority: "TriageAuthorityStore",
    issue: "Issue",
) -> tuple[int, ...]:
    """The act-level cohort a health review owns (#6780).

    Single owner for "what may this review act on", with two ordered sources —
    both DEDICATED cohort surfaces, never the board snapshot:

    1. the producer's grant, when the review was launched from the pending
       queue (the normal path). The queued item knows its own cohort;
    2. otherwise the durable cohort ledger, keyed by the anchor issue. A
       marker-labeled anchor can also be picked up as an ordinary issue, which
       carries no grant — reading the ledger keeps a storm anchor's authority
       exact on that path too, rather than silently dropping it.

    The two cannot disagree: intake persists to the ledger BEFORE stamping the
    queue item, and startup recovery hydrates the queue item FROM the ledger.

    Returns empty for a periodic health review — it owns no cohort, so it may
    propose but not act. Reading authority from ``BoardSnapshot`` instead
    (as this did before) handed it every unrelated failure on the board.
    """
    if triage_scope is not None:
        return triage_scope.problem_issue_numbers
    cohort = triage_authority.load_storm_cohort(anchor_issue_number=issue.number)
    return tuple(sorted({problem.issue_number for problem in cohort or ()}))


def prepare_triage_session_data(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    manifest_downloader: "ManifestDownloader",
    triage_authority: "TriageAuthorityStore",
    board_snapshot_provider: "BoardSnapshotProvider",
    issue: "Issue",
    ctx: "WorktreeContext",
    triage_scope: "TriageLaunchScope | None",
) -> None:
    """Prepare per-flavor triage session inputs (ADR-0031).

    BATCH_REVIEW keeps the existing PR-manifest prep; FAILURE_INVESTIGATION
    and HEALTH_REVIEW must NOT receive the global batch manifest (auditing
    unrelated PRs from a focused investigation was the #6768 B4 defect; a
    health review walks the board snapshot, not a PR batch). Every flavor
    gets a triage-assignment.json copy for the AGENT to read, and — the
    trusted half — an orchestrator-owned :class:`TriageLaunchAuthority`
    record persisted outside the agent-writable worktree, keyed by this
    run's identity, which completion reads as the only scope authority
    (#6761 re-review F1). Health reviews record no focus/manifest scope plus
    their OWNED problem cohort (#6780); act-level proposals may target only
    that cohort.

    Flavor resolution: an explicit ``triage_scope`` wins (the pending-queue
    launch path forwards the producer-declared grant); otherwise the
    ADR-0031 §4 marker label on the anchor issue selects HEALTH_REVIEW
    (labels are the crash-safe truth a restart recovers from); otherwise
    BATCH_REVIEW.
    """
    if not is_triage_session(config.triage_review_agent, issue.agent_type):
        return
    flavor = (triage_scope.flavor if triage_scope is not None else None) or (
        TriageSessionFlavor.HEALTH_REVIEW
        if HEALTH_REVIEW_MARKER_LABEL in issue.labels
        else TriageSessionFlavor.BATCH_REVIEW
    )
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
    focus_issue = issue.number if focused else None
    problem_issue_numbers = (
        _resolve_health_review_cohort(
            triage_scope, triage_authority=triage_authority, issue=issue
        )
        if flavor is TriageSessionFlavor.HEALTH_REVIEW
        else ()
    )
    board_snapshot = board_snapshot_provider.snapshot(
        focus_issue, problem_issue_numbers
    )
    triage_authority.record(
        run_id=ctx.run.run_id,
        session_name=ctx.run.session_name,
        authority=TriageLaunchAuthority(
            flavor=flavor,
            anchor_issue_number=issue.number,
            focus_issue_number=issue.number if focused else None,
            manifest_pr_numbers=tuple(pr.number for pr in triage_manifest.prs)
            if triage_manifest
            else (),
            problem_issue_numbers=problem_issue_numbers,
        ),
    )
    logger.info("[triage] Wrote %s assignment: %s", flavor.value, assignment_path)
    _write_board_snapshot(
        ctx,
        run_dir,
        board_snapshot,
    )
    _stage_evidence_map(
        config=config,
        repository_host=repository_host,
        ctx=ctx,
        run_dir=run_dir,
        flavor=flavor,
        focus_issue_number=focus_issue,
        board_snapshot=board_snapshot,
    )


def _write_board_snapshot(
    ctx: "WorktreeContext",
    run_dir: Path,
    snapshot: BoardSnapshot,
) -> None:
    """Write the ADR-0031 §3 board snapshot into the triage-data directory.

    The triage prompt treats board-snapshot.json as authoritative required
    input, so build/write failures propagate and fail the launch loudly
    (fail-fast: a DB/log bug must not silently launch a session missing its
    input — the launcher converts the exception into a failed LaunchResult).
    The run-manifest entry is recorded only after a successful write so it
    never points at a missing file.
    """
    snapshot_path = run_dir / "triage-data" / BOARD_SNAPSHOT_FILENAME
    snapshot.write(snapshot_path)
    ctx.update_manifest({"board_snapshot": str(snapshot_path)})


def _focus_failure_artifact_hints(
    board_snapshot: BoardSnapshot, focus_issue_number: int
) -> tuple[str, ...]:
    """Artifact-hint paths on the focus issue's board failure, if present.

    The board snapshot already carries recent failures with their on-disk
    artifact hints; the focus issue's failure (when on the board) supplies the
    run-dir locations the investigation should start from. Returns empty when
    the focus issue is not among the recent failures — the sibling-worktree
    glob in :func:`triage_evidence.build_evidence_map` still finds its run-dirs.
    """
    for failure in board_snapshot.recent_failures:
        if failure.issue_number == focus_issue_number:
            return tuple(failure.artifact_hints)
    return ()


def _stage_evidence_map(
    *,
    config: "Config",
    repository_host: "RepositoryHost",
    ctx: "WorktreeContext",
    run_dir: Path,
    flavor: TriageSessionFlavor,
    focus_issue_number: int | None,
    board_snapshot: BoardSnapshot,
) -> None:
    """Best-effort: stage the read-side evidence map for a triage session.

    Unlike :func:`_write_board_snapshot` (fail-fast, because board-snapshot.json
    is a REQUIRED agent input), the evidence map is an ENHANCEMENT — a
    deliberate exception to the fail-fast house style. The whole build+write is
    wrapped so ANY failure only logs a warning and continues: failing to stage
    evidence must never fail the session launch. The manifest entry is recorded
    only after a successful write, so it never points at a missing file.

    Per flavor: BATCH_REVIEW gets no evidence map (it audits a PR batch, not
    orchestrator-state facts); FAILURE_INVESTIGATION gets the full focus map
    (the god-view substrate + the focus issue's own run-dirs + a GitHub
    warm-cache); HEALTH_REVIEW gets the full SYSTEM map — the same substrate
    (all SQLite stores, roots) plus whole-system run-dirs enumerated across
    every worktree, since it assesses the whole floor and has no single focus
    (``build_evidence_map`` keys both off ``focus_issue_number`` being None).
    """
    if flavor is TriageSessionFlavor.BATCH_REVIEW:
        return
    try:
        artifact_hints = (
            _focus_failure_artifact_hints(board_snapshot, focus_issue_number)
            if focus_issue_number is not None
            else ()
        )
        evidence = build_evidence_map(
            config=config,
            repository_host=repository_host,
            focus_issue_number=focus_issue_number,
            artifact_hints=artifact_hints,
        )
        path = write_evidence_map(run_dir, evidence)
        ctx.update_manifest({"evidence_map": str(path)})
    except Exception as exc:  # noqa: BLE001 - evidence map is best-effort, never fatal
        logger.warning("[triage] Evidence map staging failed (non-fatal): %s", exc)
